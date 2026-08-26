"""LangGraph wiring: route → retrieve → synthesize."""

from __future__ import annotations

from functools import partial
from typing import Any

from langgraph.graph import END, START, StateGraph

from src.agent.nodes import retrieve_evidence, route_sources, synthesize_answer
from src.agent.state import AgentState
from src.retrieval.retriever import SourceRetriever


def build_agent(retriever: SourceRetriever | None = None):
    retriever = retriever or SourceRetriever()
    retrieve_node = partial(retrieve_evidence, retriever=retriever)

    graph = StateGraph(AgentState)
    graph.add_node("route", route_sources)
    graph.add_node("retrieve", retrieve_node)
    graph.add_node("synthesize", synthesize_answer)

    graph.add_edge(START, "route")
    graph.add_edge("route", "retrieve")
    graph.add_edge("retrieve", "synthesize")
    graph.add_edge("synthesize", END)

    return graph.compile()


def run_agent(question: str, retriever: SourceRetriever | None = None) -> dict[str, Any]:
    agent = build_agent(retriever)
    result = agent.invoke({"question": question})
    return {
        "question": question,
        "answer": result.get("answer", ""),
        "selected_sources": result.get("selected_sources", []),
        "routing_rationale": result.get("routing_rationale", ""),
        "citations": result.get("citations", []),
        "sub_queries": result.get("sub_queries", []),
    }
