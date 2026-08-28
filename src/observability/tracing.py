"""Langfuse tracing — every `/ask` becomes an inspectable trace.

Tracing is **opt-in and inert by default**: with no keys configured, every
function here is a no-op and the project runs exactly as it does without
Langfuse installed. Observability should never be a dependency of the thing it
observes.

What LangChain instruments for free is the LLM calls — routing and synthesis.
What it cannot see is retrieval, because the OpenSearch backend is our code
rather than a LangChain component. That is precisely the part worth watching:
when an answer is wrong, the first question is always *what did it actually
read?* So `record_retrieval` logs the chunks and their scores explicitly.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterator, Sequence
from typing import Any

from src.config import Settings, get_settings

logger = logging.getLogger(__name__)

_client: Any | None = None
_enabled = False

# Chunk text stored per retrieved result. Enough to recognise what was read
# without turning every trace into a copy of the corpus.
PREVIEW_CHARS = 300


def setup_tracing(settings: Settings | None = None) -> bool:
    """Initialise Langfuse if keys are present. Safe to call more than once.

    Returns whether tracing is on, so callers can log it rather than guess.
    """
    global _client, _enabled
    settings = settings or get_settings()

    if not settings.tracing_enabled:
        logger.info("tracing off (no Langfuse keys configured)")
        return False
    if _enabled:
        return True

    try:
        from langfuse import Langfuse

        _client = Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            host=settings.langfuse_url,
        )
        if not _client.auth_check():
            logger.warning("Langfuse rejected the credentials — tracing stays off")
            _client = None
            return False
    except Exception as exc:  # noqa: BLE001 - tracing must never break the app
        logger.warning("could not start Langfuse (%s) — tracing stays off", exc)
        _client = None
        return False

    _enabled = True
    logger.info("tracing on → %s", settings.langfuse_url)
    return True


def is_tracing_enabled() -> bool:
    return _enabled


def langfuse_callbacks() -> list[Any]:
    """Callback handlers to hand LangChain/LangGraph. Empty when tracing is off.

    Passing these into `graph.invoke(config={"callbacks": ...})` is what makes
    the routing and synthesis LLM calls appear as spans, with their prompts,
    token counts, and latency.
    """
    if not _enabled:
        return []
    try:
        from langfuse.langchain import CallbackHandler

        return [CallbackHandler()]
    except Exception as exc:  # noqa: BLE001
        logger.warning("Langfuse callback unavailable: %s", exc)
        return []


@contextlib.contextmanager
def traced(name: str, as_type: str = "span", **fields: Any) -> Iterator[None]:
    """Wrap a block as one span. A plain no-op when tracing is off."""
    if not _enabled or _client is None:
        yield
        return
    try:
        with _client.start_as_current_observation(name=name, as_type=as_type, **fields):
            yield
    except Exception as exc:  # noqa: BLE001 - never let a trace break the request
        logger.debug("span %r failed: %s", name, exc)
        yield


def record_retrieval(
    query: str,
    sources: Sequence[str] | None,
    chunks: Sequence[Any],
    *,
    min_similarity: float | None = None,
) -> None:
    """Attach what retrieval returned to the current span.

    Scores are the point. An answer that went wrong is either bad retrieval or
    bad generation, and the scores are what separate the two: high scores with a
    wrong answer means the model ignored good evidence; low scores mean nothing
    relevant was found and it should have refused.
    """
    if not _enabled or _client is None:
        return
    try:
        _client.update_current_span(
            input={"query": query, "sources": list(sources) if sources else "all"},
            output=[
                {
                    "source": c.source,
                    "title": c.title,
                    "score": round(c.score, 4) if c.score is not None else None,
                    "url": c.url,
                    "text": c.content[:PREVIEW_CHARS],
                }
                for c in chunks
            ],
            metadata={
                "returned": len(chunks),
                "min_similarity": min_similarity,
                "top_score": round(chunks[0].score, 4) if chunks and chunks[0].score else None,
                # No results is the single most useful thing to alert on: it is
                # either an off-corpus question or retrieval quietly breaking.
                "empty": not chunks,
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("could not record retrieval: %s", exc)


def flush() -> None:
    """Send anything buffered. Worth calling before a short-lived process exits."""
    if _enabled and _client is not None:
        with contextlib.suppress(Exception):
            _client.flush()
