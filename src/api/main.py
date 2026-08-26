"""FastAPI entrypoint for the knowledge agent."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from src.agent.graph import run_agent
from src.config import get_settings
from src.retrieval.retriever import SourceRetriever

logger = logging.getLogger(__name__)

_retriever: SourceRetriever | None = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    global _retriever
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)
    try:
        _retriever = SourceRetriever()
        logger.info("FAISS index loaded")
    except FileNotFoundError as exc:
        logger.warning("%s — /ask will fail until you run ingestion", exc)
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
def health() -> dict[str, str]:
    return {"status": "ok", "index": "ready" if _retriever else "missing"}


@app.post("/ask", response_model=AskResponse)
def ask(body: AskRequest) -> AskResponse:
    if _retriever is None:
        raise HTTPException(
            status_code=503,
            detail="Vector index not built. Run: python -m src.ingestion.pipeline",
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
