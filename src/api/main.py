"""FastAPI entrypoint for the knowledge agent."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from src.agent.graph import run_agent
from src.config import get_settings
from src.observability import is_tracing_enabled, setup_tracing
from src.retrieval.retriever import SourceRetriever

logger = logging.getLogger(__name__)

_retriever: SourceRetriever | None = None
_chunk_count = 0


@asynccontextmanager
async def lifespan(_: FastAPI):
    global _retriever, _chunk_count
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)
    logging.getLogger("faiss.loader").setLevel(logging.WARNING)
    setup_tracing(settings)
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
    }


@app.post("/ask", response_model=AskResponse)
def ask(body: AskRequest) -> AskResponse:
    if _retriever is None:
        raise HTTPException(
            status_code=503,
            detail="Vector store unavailable or empty. Run: python -m src.ingestion.pipeline",
        )
    result = run_agent(body.question, retriever=_retriever)
    return AskResponse(**result)


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
