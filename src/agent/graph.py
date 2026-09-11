"""LangGraph wiring: route → retrieve → synthesize.

One shape, built two ways. The synchronous graph serves the evaluation harness
and scripts; the asynchronous graph serves the API, where a request spends
nearly all its time waiting on the network and should not hold a thread while
it waits.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from langgraph.graph import END, START, StateGraph

from src.agent.nodes import (
    _text,
    aretrieve_evidence,
    aroute_sources,
    asynthesize_answer,
    retrieve_evidence,
    route_sources,
    synthesize_answer,
)
from src.agent.state import AgentState
from src.observability import langfuse_callbacks
from src.retrieval.retriever import SourceRetriever


def build_agent(retriever: SourceRetriever | None = None, *, asynchronous: bool = False):
    retriever = retriever or SourceRetriever()
    graph = StateGraph(AgentState)

    if asynchronous:
        async def retrieve(state: AgentState) -> AgentState:
            return await aretrieve_evidence(state, retriever)

        graph.add_node("route", aroute_sources)
        graph.add_node("retrieve", retrieve)
        graph.add_node("synthesize", asynthesize_answer)
    else:
        def retrieve(state: AgentState) -> AgentState:
            return retrieve_evidence(state, retriever)

        graph.add_node("route", route_sources)
        graph.add_node("retrieve", retrieve)
        graph.add_node("synthesize", synthesize_answer)

    graph.add_edge(START, "route")
    graph.add_edge("route", "retrieve")
    graph.add_edge("retrieve", "synthesize")
    graph.add_edge("synthesize", END)
    return graph.compile()


def _config() -> dict[str, Any]:
    # The callbacks are what make the routing and synthesis model calls show up
    # as traced spans, with their prompts and token counts. Empty when tracing
    # is off.
    return {"callbacks": langfuse_callbacks(), "run_name": "ask"}


def _result(question: str, state: dict[str, Any]) -> dict[str, Any]:
    return {
        "question": question,
        "answer": state.get("answer", ""),
        "selected_sources": state.get("selected_sources", []),
        "routing_rationale": state.get("routing_rationale", ""),
        "citations": state.get("citations", []),
        "sub_queries": state.get("sub_queries", []),
    }


def run_agent(question: str, retriever: SourceRetriever | None = None) -> dict[str, Any]:
    state = build_agent(retriever).invoke({"question": question}, config=_config())
    return _result(question, state)


async def arun_agent(question: str, retriever: SourceRetriever | None = None) -> dict[str, Any]:
    agent = build_agent(retriever, asynchronous=True)
    state = await agent.ainvoke({"question": question}, config=_config())
    return _result(question, state)


async def astream_agent(
    question: str, retriever: SourceRetriever | None = None
) -> AsyncIterator[tuple[str, Any]]:
    """Run the agent, reporting progress as it happens.

    Yields, in order:
        ("routed",    {"sources", "sub_queries", "rationale"})
        ("retrieved", {"evidence": count})
        ("token",     text)     zero or more, as the answer is written
        ("done",      result)   the same shape `run_agent` returns

    Nothing here re-implements the agent. LangGraph already streams two things:
    "updates" reports each node as it finishes, and "messages" reports model
    tokens as they arrive. This subscribes to both, and keeps only the tokens
    from synthesis — the router's tokens are raw JSON, of no use to a reader.
    """
    agent = build_agent(retriever, asynchronous=True)
    state: dict[str, Any] = {"question": question}
    wrote_tokens = False

    async for mode, chunk in agent.astream(
        {"question": question}, config=_config(), stream_mode=["updates", "messages"]
    ):
        if mode == "messages":
            message, metadata = chunk
            if metadata.get("langgraph_node") == "synthesize" and message.content:
                wrote_tokens = True
                yield "token", _text(message.content)
            continue

        for node, update in chunk.items():
            state.update(update or {})
            if node == "route":
                yield "routed", {
                    "sources": state.get("selected_sources", []),
                    "sub_queries": state.get("sub_queries", []),
                    "rationale": state.get("routing_rationale", ""),
                }
            elif node == "retrieve":
                yield "retrieved", {"evidence": len(state.get("retrieved") or [])}
            elif node == "synthesize" and not wrote_tokens:
                # No tokens means no model call — the refusal path. The reader
                # still needs to see the answer arrive.
                yield "token", state.get("answer", "")

    yield "done", _result(question, state)
