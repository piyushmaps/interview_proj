"""LangGraph agent state."""

from typing import Any, TypedDict

from src.retrieval.retriever import RetrievedChunk


class AgentState(TypedDict, total=False):
    question: str
    selected_sources: list[str]
    sub_queries: list[str]
    retrieved: list[RetrievedChunk]
    answer: str
    citations: list[dict[str, Any]]
    routing_rationale: str
