"""FastAPI entrypoint for the knowledge agent.

Both question endpoints are asynchronous. A request spends nearly all its time
waiting — on the router model, the embedding service, the vector store, the
answer model — and an asynchronous handler waits without holding a worker
thread. That is what lets one process carry many requests at once, instead of
queueing everything past its thread-pool size.

`/ask` returns the finished answer. `/ask/stream` returns the same answer as it
is being written, as server-sent events.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from src.agent.graph import arun_agent, astream_agent
from src.api.cache import AnswerCache
from src.config import get_settings
from src.observability import is_tracing_enabled, setup_tracing
from src.retrieval.retriever import SourceRetriever

logger = logging.getLogger(__name__)

_retriever: SourceRetriever | None = None
_chunk_count = 0
_cache: AnswerCache | None = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    global _retriever, _chunk_count, _cache
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)
    logging.getLogger("faiss.loader").setLevel(logging.WARNING)
    setup_tracing(settings)
    if settings.cache_enabled:
        _cache = AnswerCache(
            max_entries=settings.cache_max_entries,
            ttl_seconds=settings.cache_ttl_seconds,
            similarity=settings.cache_similarity,
        )
        logger.info(
            "answer cache on (%d entries, %.0fs ttl, %.2f similarity)",
            settings.cache_max_entries, settings.cache_ttl_seconds, settings.cache_similarity,
        )
    try:
        _retriever = SourceRetriever()
        _chunk_count = _retriever.store.count()
        logger.info("vector store ready: %s, %d chunks", settings.vector_store, _chunk_count)
        if _chunk_count == 0:
            logger.warning("index is empty — run: python -m src.ingestion.pipeline")
    except Exception as exc:  # noqa: BLE001 - surfaced through /health instead
        logger.warning("vector store unavailable (%s) — /ask will fail until it is", exc)
        _retriever = None
    yield


app = FastAPI(
    title="Intuit Assist — Agentic RAG",
    description="Multi-source capability knowledge agent (Jira, Slack, DevPortal, City Map)",
    version="0.1.0",
    lifespan=lifespan,
)


class AskRequest(BaseModel):
    question: str = Field(..., min_length=3, examples=["How do I create a new capability?"])


class AskResponse(BaseModel):
    question: str
    answer: str
    selected_sources: list[str]
    routing_rationale: str
    citations: list[dict[str, Any]]
    sub_queries: list[str]


@app.get("/health")
def health() -> dict[str, Any]:
    settings = get_settings()
    return {
        "status": "ok" if _retriever and _chunk_count else "degraded",
        "vector_store": settings.vector_store,
        "chunks": _chunk_count,
        "index": "ready" if _retriever and _chunk_count else "empty or unavailable",
        "tracing": "on" if is_tracing_enabled() else "off",
        "cache": _cache.stats.as_dict() | {"entries": len(_cache)} if _cache else "off",
    }


def _require_retriever() -> SourceRetriever:
    if _retriever is None:
        raise HTTPException(
            status_code=503,
            detail="Vector store unavailable or empty. Run: python -m src.ingestion.pipeline",
        )
    return _retriever


async def _cache_lookup(
    retriever: SourceRetriever, question: str
) -> tuple[Sequence[float] | None, dict[str, Any] | None]:
    """Embed the question and check the cache. Returns (vector, cached answer).

    The embedding is what lets "who owns Payments" reuse the entry stored for
    "who owns the Payments capability". A cache must never fail a request, so
    an embedding error means serving uncached, not an error.

    Reading and writing the cache needs no lock on this path: every request
    runs on one event loop, and `get`/`put` contain no `await`, so two requests
    can never be inside them at the same moment.
    """
    if _cache is None:
        return None, None
    try:
        vector = await retriever.embeddings.aembed_query(question)
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not embed for cache lookup (%s); serving uncached", exc)
        return None, None
    return vector, _cache.get(question, vector)


def _remember(question: str, vector: Sequence[float] | None, result: dict[str, Any]) -> None:
    if _cache is not None and vector is not None:
        _cache.put(question, vector, result)


@app.post("/ask", response_model=AskResponse)
async def ask(body: AskRequest) -> AskResponse:
    retriever = _require_retriever()
    vector, cached = await _cache_lookup(retriever, body.question)
    if cached is not None:
        return AskResponse(**cached)
    result = await arun_agent(body.question, retriever=retriever)
    _remember(body.question, vector, result)
    return AskResponse(**result)


def _event(name: str, data: dict[str, Any]) -> str:
    """One server-sent event: a named event line, a data line, a blank line."""
    return f"event: {name}\ndata: {json.dumps(data)}\n\n"


async def _answer_events(question: str, retriever: SourceRetriever) -> AsyncIterator[str]:
    """The answer as a stream of events, in this order:

        status   work has started              — arrives immediately
        status   sources chosen, searching
        status   evidence found, writing
        token    a piece of the answer         — repeated, as it is written
        done     the full result, with citations

    Citations come last on purpose. Which evidence the answer used is only known
    once the answer is finished, so they cannot be sent any earlier.
    """
    try:
        yield _event("status", {"stage": "started"})

        vector, cached = await _cache_lookup(retriever, question)
        if cached is not None:
            yield _event("token", {"text": cached["answer"]})
            yield _event("done", {**cached, "cached": True})
            return

        async for kind, data in astream_agent(question, retriever=retriever):
            if kind == "routed":
                yield _event("status", {"stage": "searching", **data})
            elif kind == "retrieved":
                yield _event("status", {"stage": "writing", **data})
            elif kind == "token":
                yield _event("token", {"text": data})
            elif kind == "done":
                _remember(question, vector, data)
                yield _event("done", {**data, "cached": False})
    except Exception as exc:
        # The response headers have already gone out with a 200, so the status
        # code can no longer change. The failure has to travel inside the stream.
        logger.exception("streamed answer failed")
        yield _event("error", {"message": str(exc)})


@app.post("/ask/stream")
async def ask_stream(body: AskRequest) -> StreamingResponse:
    retriever = _require_retriever()
    return StreamingResponse(
        _answer_events(body.question, retriever),
        media_type="text/event-stream",
        # Stop proxies from buffering the stream, which would defeat the point.
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/cache/clear")
def clear_cache() -> dict[str, str]:
    """Drop cached answers. Call after re-ingestion, since entries were built
    against the previous index."""
    if _cache is None:
        return {"status": "cache is disabled"}
    _cache.clear()
    return {"status": "cleared"}


def run() -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "src.api.main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=True,
    )


if __name__ == "__main__":
    run()
