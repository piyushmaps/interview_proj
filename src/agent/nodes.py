"""Agent nodes: route → retrieve → synthesize.

Each node exists twice: a synchronous version for the evaluation harness and
scripts, and an asynchronous one for the API. They share every piece of logic
that is not the call itself — prompt building, parsing, deduplication, citation
filtering — so the two cannot drift apart. The only difference between a pair
is `invoke` versus `await ainvoke`.
"""

from __future__ import annotations

import asyncio
import json
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from langchain_openai import ChatOpenAI

from src.agent.state import AgentState
from src.config import get_settings
from src.models.document import SourceType
from src.observability import record_retrieval, traced
from src.retrieval.retriever import RetrievedChunk, SourceRetriever

VALID_SOURCES = {s.value for s in SourceType}

REFUSAL = (
    "I couldn't find relevant evidence in Jira, Slack, DevPortal, or the city map. "
    "Try rephrasing or asking about capability creation, ownership, or open tickets."
)


def _llm() -> ChatOpenAI:
    settings = get_settings()
    return ChatOpenAI(
        model=settings.openai_model,
        api_key=settings.openai_api_key,
        temperature=0,
    )


def _text(content: Any) -> str:
    return content if isinstance(content, str) else str(content)


# -- route --------------------------------------------------------------------


def _routing_prompt(question: str) -> str:
    return f"""You are a routing layer for an internal knowledge agent.
Available sources:
- jira: tickets, decisions, open work
- slack: tribal Q&A, how-tos from engineers
- devportal: official documentation and checklists
- city_map: ownership, dependencies, capability graph

Question: {question}

Respond with ONLY valid JSON (no markdown):
{{
  "sources": ["devportal", "slack"],
  "sub_queries": ["optional focused queries if the question has multiple parts"],
  "rationale": "one sentence why these sources"
}}

Rules:
- Pick 1–3 sources most likely to answer.
- Ownership / who-owns / dependencies → include city_map.
- How-to / checklist / process → include devportal; often also slack.
- Tickets / status / open work → include jira.
- If unsure, include devportal + city_map.
"""


def _apply_routing(state: AgentState, raw: Any) -> AgentState:
    """Turn the router's reply into state. Never trusts it as a control signal:
    unknown sources are dropped, and an unusable reply falls back to a default."""
    parsed = _parse_json(_text(raw))
    sources = [s for s in parsed.get("sources", []) if s in VALID_SOURCES]
    return {
        **state,
        "selected_sources": sources or ["devportal", "city_map"],
        "sub_queries": parsed.get("sub_queries") or [state["question"]],
        "routing_rationale": parsed.get("rationale", ""),
    }


def route_sources(state: AgentState) -> AgentState:
    """Decide which sources to query and optional sub-queries."""
    return _apply_routing(state, _llm().invoke(_routing_prompt(state["question"])).content)


async def aroute_sources(state: AgentState) -> AgentState:
    message = await _llm().ainvoke(_routing_prompt(state["question"]))
    return _apply_routing(state, message.content)


# -- retrieve -----------------------------------------------------------------


def _retrieval_plan(state: AgentState) -> tuple[list[str], list[str]]:
    return (
        state.get("selected_sources") or list(VALID_SOURCES),
        state.get("sub_queries") or [state["question"]],
    )


def _record(
    sub_queries: list[str],
    per_query: list[list[RetrievedChunk]],
    sources: list[str],
    retriever: SourceRetriever,
) -> None:
    # Recorded after the searches return rather than inside them: tracing
    # context does not cross a thread boundary, and the data is what matters,
    # not which thread produced it.
    for sub_query, chunks in zip(sub_queries, per_query, strict=True):
        with traced(f"search: {sub_query[:60]}", as_type="retriever"):
            record_retrieval(
                sub_query, sources, chunks,
                min_similarity=getattr(retriever, "min_similarity", None),
            )


def _dedupe(per_query: list[list[RetrievedChunk]]) -> list[RetrievedChunk]:
    """Merge in sub-query order, so output is stable whatever finished first."""
    seen: set[tuple[str, str]] = set()
    merged: list[RetrievedChunk] = []
    for chunks in per_query:
        for chunk in chunks:
            key = (chunk.url, chunk.content[:120])
            if key not in seen:
                seen.add(key)
                merged.append(chunk)
    return merged


def retrieve_evidence(state: AgentState, retriever: SourceRetriever) -> AgentState:
    """Retrieve concurrently across sub-queries, filtered to selected sources."""
    sources, sub_queries = _retrieval_plan(state)
    with traced("retrieve", as_type="span", input={"sub_queries": sub_queries, "sources": sources}):
        # Sub-queries are independent, input/output-bound searches.
        with ThreadPoolExecutor(max_workers=min(len(sub_queries), 4) or 1) as pool:
            per_query = list(pool.map(lambda q: retriever.retrieve(q, sources=sources), sub_queries))
        _record(sub_queries, per_query, sources, retriever)
    return {**state, "retrieved": _dedupe(per_query)}


async def aretrieve_evidence(state: AgentState, retriever: SourceRetriever) -> AgentState:
    sources, sub_queries = _retrieval_plan(state)
    with traced("retrieve", as_type="span", input={"sub_queries": sub_queries, "sources": sources}):
        # Same concurrency as the thread pool above, without a thread per search.
        per_query = list(
            await asyncio.gather(*(retriever.aretrieve(q, sources=sources) for q in sub_queries))
        )
        _record(sub_queries, per_query, sources, retriever)
    return {**state, "retrieved": _dedupe(per_query)}


# -- synthesize ---------------------------------------------------------------


def _evidence_prompt(state: AgentState) -> tuple[str, list[dict[str, Any]]]:
    """The synthesis prompt, and the full citation list it numbers."""
    blocks: list[str] = []
    citations: list[dict[str, Any]] = []
    for i, chunk in enumerate(state["retrieved"], start=1):
        blocks.append(
            f"[{i}] source={chunk.source} title={chunk.title}\nurl={chunk.url}\n{chunk.content}"
        )
        citations.append(
            {"index": i, "source": chunk.source, "title": chunk.title, "url": chunk.url}
        )

    evidence = "\n\n".join(blocks)
    prompt = f"""You are Intuit Assist, an internal engineering knowledge agent.
Answer using ONLY the evidence below. Cite sources as [1], [2], etc.
If evidence conflicts, prefer DevPortal over Slack, and city map for ownership facts.
Be concise and actionable.

Question: {state['question']}

Selected sources: {', '.join(state.get('selected_sources') or [])}
Routing: {state.get('routing_rationale') or 'n/a'}

Evidence:
{evidence}

Answer:
"""
    return prompt, citations


def _cited_only(answer: str, citations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The evidence the answer actually cites, not everything retrieved —
    otherwise a two-source answer renders top_k links. Original indices are
    kept so [n] markers still resolve. If nothing was cited, show everything
    rather than nothing."""
    cited = {int(n) for n in re.findall(r"\[(\d+)\]", answer)}
    return [c for c in citations if c["index"] in cited] if cited else citations


def _refused(state: AgentState) -> AgentState:
    # No evidence cleared the relevance cutoff, so the model is never called:
    # there is nothing for it to answer from, and nothing to hallucinate with.
    return {**state, "answer": REFUSAL, "citations": []}


def synthesize_answer(state: AgentState) -> AgentState:
    """Grounded answer with citations."""
    if not state.get("retrieved"):
        return _refused(state)
    prompt, citations = _evidence_prompt(state)
    answer = _text(_llm().invoke(prompt).content)
    return {**state, "answer": answer, "citations": _cited_only(answer, citations)}


async def asynthesize_answer(state: AgentState) -> AgentState:
    if not state.get("retrieved"):
        return _refused(state)
    prompt, citations = _evidence_prompt(state)
    answer = _text((await _llm().ainvoke(prompt)).content)
    return {**state, "answer": answer, "citations": _cited_only(answer, citations)}


def _parse_json(text: str) -> dict:
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            return json.loads(match.group(0))
        return {}
