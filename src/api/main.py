"""FastAPI entrypoint for the knowledge agent."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from src.agent.graph import run_agent
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


@app.post("/ask", response_model=AskResponse)
def ask(body: AskRequest) -> AskResponse:
    if _retriever is None:
        raise HTTPException(
            status_code=503,
            detail="Vector store unavailable or empty. Run: python -m src.ingestion.pipeline",
        )
    question = body.question

    if _cache is None:
        return AskResponse(**run_agent(question, retriever=_retriever))

    # Embedding the question here is not extra work in spirit — retrieval needs
    # it anyway on a miss — and it is what lets "who owns Payments" hit the
    # entry stored for "who owns the Payments capability".
    try:
        vector = _retriever.embeddings.embed_query(question)
    except Exception as exc:  # noqa: BLE001 - a cache must never fail a request
        logger.warning("could not embed for cache lookup (%s); serving uncached", exc)
        return AskResponse(**run_agent(question, retriever=_retriever))

    cached = _cache.get(question, vector)
    if cached is not None:
        return AskResponse(**cached)

    result = run_agent(question, retriever=_retriever)
    _cache.put(question, vector, result)
    return AskResponse(**result)


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
