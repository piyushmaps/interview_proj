"""Agent nodes: route → retrieve → synthesize."""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor

from langchain_openai import ChatOpenAI

from src.agent.state import AgentState
from src.config import get_settings
from src.models.document import SourceType
from src.retrieval.retriever import SourceRetriever

VALID_SOURCES = {s.value for s in SourceType}


def _llm() -> ChatOpenAI:
    settings = get_settings()
    return ChatOpenAI(
        model=settings.openai_model,
        api_key=settings.openai_api_key,
        temperature=0,
    )


def route_sources(state: AgentState) -> AgentState:
    """Decide which sources to query and optional sub-queries."""
    question = state["question"]
    prompt = f"""You are a routing layer for an internal knowledge agent.
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
    raw = _llm().invoke(prompt).content
    parsed = _parse_json(raw if isinstance(raw, str) else str(raw))

    sources = [s for s in parsed.get("sources", []) if s in VALID_SOURCES]
    if not sources:
        sources = ["devportal", "city_map"]

    sub_queries = parsed.get("sub_queries") or [question]
    if not sub_queries:
        sub_queries = [question]

    return {
        **state,
        "selected_sources": sources,
        "sub_queries": sub_queries,
        "routing_rationale": parsed.get("rationale", ""),
    }


def retrieve_evidence(state: AgentState, retriever: SourceRetriever) -> AgentState:
    """Retrieve concurrently across sub-queries, filtered to selected sources."""
    sources = state.get("selected_sources") or list(VALID_SOURCES)
    sub_queries = state.get("sub_queries") or [state["question"]]

    # Sub-queries are independent, I/O-bound searches — run them concurrently.
    with ThreadPoolExecutor(max_workers=min(len(sub_queries), 4) or 1) as pool:
        per_query = list(pool.map(lambda q: retriever.retrieve(q, sources=sources), sub_queries))

    # Dedupe in sub-query order so output stays stable regardless of completion order.
    seen: set[tuple[str, str]] = set()
    retrieved = []
    for chunks in per_query:
        for chunk in chunks:
            key = (chunk.url, chunk.content[:120])
            if key in seen:
                continue
            seen.add(key)
            retrieved.append(chunk)

    return {**state, "retrieved": retrieved}


def synthesize_answer(state: AgentState) -> AgentState:
    """Grounded answer with citations."""
    chunks = state.get("retrieved") or []
    if not chunks:
        return {
            **state,
            "answer": (
                "I couldn't find relevant evidence in Jira, Slack, DevPortal, or the city map. "
                "Try rephrasing or asking about capability creation, ownership, or open tickets."
            ),
            "citations": [],
        }

    evidence_blocks = []
    citations = []
    for i, chunk in enumerate(chunks, start=1):
        evidence_blocks.append(
            f"[{i}] source={chunk.source} title={chunk.title}\nurl={chunk.url}\n{chunk.content}"
        )
        citations.append(
            {
                "index": i,
                "source": chunk.source,
                "title": chunk.title,
                "url": chunk.url,
            }
        )

    evidence = "\n\n".join(evidence_blocks)
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
    answer = _llm().invoke(prompt).content
    answer_text = answer if isinstance(answer, str) else str(answer)

    # Return the evidence the answer actually cites, not everything retrieved —
    # otherwise a two-source answer renders top_k citation links. Keep the
    # original indices so [n] markers in the text still resolve. If the model
    # cited nothing, fall back to the full set rather than showing none.
    cited = {int(n) for n in re.findall(r"\[(\d+)\]", answer_text)}
    if cited:
        citations = [c for c in citations if c["index"] in cited]

    return {**state, "answer": answer_text, "citations": citations}


def _parse_json(text: str) -> dict:
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            return json.loads(match.group(0))
        return {}
