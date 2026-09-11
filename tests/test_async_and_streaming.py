"""The asynchronous agent and the streaming endpoint.

A fake chat model stands in for the real one. It streams its reply word by
word through the same callback machinery the real model uses, so these tests
exercise the actual streaming path — not a mock of it.
"""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from src.agent import graph, nodes
from src.api import main
from src.retrieval.retriever import SourceRetriever
from src.vectorstore.base import Chunk, SearchHit

ROUTE_REPLY = (
    '{"sources": ["city_map"], "sub_queries": ["who owns Payments?"], '
    '"rationale": "ownership question"}'
)
ANSWER = "Payments is owned by the money-movement team [1]."


class StubStore:
    def __init__(self, hits: list[SearchHit]) -> None:
        self.hits = hits

    def search(self, vector, k, sources=None, query_text=None):
        return self.hits[:k]

    def ensure_ready(self, dim): ...
    def upsert(self, chunks, vectors): return 0
    def delete_docs(self, doc_ids): return 0
    def count(self): return len(self.hits)
    def persist(self): ...


class StubEmbeddings:
    def embed_query(self, text: str) -> list[float]:
        return [1.0, 0.0]

    async def aembed_query(self, text: str) -> list[float]:
        return [1.0, 0.0]


def _hit(cid: str, text: str, score: float) -> SearchHit:
    return SearchHit(
        chunk=Chunk(id=cid, doc_id=cid, content=text, source="city_map",
                    title=f"City Map: {cid}", url=f"https://citymap.example.com/{cid}"),
        score=score,
    )


def _retriever(*scores: float) -> SourceRetriever:
    hits = [_hit(f"doc{i}", f"evidence {i}: owner team money-movement", s)
            for i, s in enumerate(scores, start=1)]
    return SourceRetriever(store=StubStore(hits), embeddings=StubEmbeddings())


def _fake_llm(monkeypatch, *replies: str) -> None:
    """Every call to the model gets the next reply, in order."""
    model = GenericFakeChatModel(messages=iter([AIMessage(content=r) for r in replies]))
    monkeypatch.setattr(nodes, "_llm", lambda: model)


def _collect(question: str, retriever: SourceRetriever) -> list[tuple[str, object]]:
    async def run():
        return [event async for event in graph.astream_agent(question, retriever=retriever)]
    return asyncio.run(run())


# -- asynchronous nodes -------------------------------------------------------


def test_async_router_parses_its_reply(monkeypatch):
    _fake_llm(monkeypatch, ROUTE_REPLY)
    state = asyncio.run(nodes.aroute_sources({"question": "who owns Payments?"}))
    assert state["selected_sources"] == ["city_map"]
    assert state["sub_queries"] == ["who owns Payments?"]


def test_async_synthesis_returns_only_cited_evidence(monkeypatch):
    _fake_llm(monkeypatch, ANSWER)
    retrieved = _retriever(0.9, 0.8).retrieve("q")
    state = asyncio.run(nodes.asynthesize_answer({"question": "q", "retrieved": retrieved}))
    assert [c["index"] for c in state["citations"]] == [1]


def test_async_refusal_never_calls_the_model(monkeypatch):
    monkeypatch.setattr(nodes, "_llm", lambda: pytest.fail("the model must not be called"))
    state = asyncio.run(nodes.asynthesize_answer({"question": "q", "retrieved": []}))
    assert state["answer"] == nodes.REFUSAL
    assert state["citations"] == []


def test_async_retrieval_applies_the_relevance_cutoff():
    chunks = asyncio.run(_retriever(0.9, 0.1).aretrieve("q", min_similarity=0.28))
    assert len(chunks) == 1


def test_synchronous_and_asynchronous_agents_agree(monkeypatch):
    """The two paths share their logic, so the same replies must give the same result."""
    _fake_llm(monkeypatch, ROUTE_REPLY, ANSWER)
    sync = graph.run_agent("who owns Payments?", retriever=_retriever(0.9))
    _fake_llm(monkeypatch, ROUTE_REPLY, ANSWER)
    async_ = asyncio.run(graph.arun_agent("who owns Payments?", retriever=_retriever(0.9)))
    assert sync == async_


# -- streaming ----------------------------------------------------------------


def test_stream_reports_progress_then_words_then_the_result(monkeypatch):
    _fake_llm(monkeypatch, ROUTE_REPLY, ANSWER)
    events = _collect("who owns Payments?", _retriever(0.9))
    kinds = [kind for kind, _ in events]

    assert kinds[0] == "routed"
    assert kinds[1] == "retrieved"
    assert kinds[-1] == "done"

    tokens = [data for kind, data in events if kind == "token"]
    # More than one token means it really arrived piece by piece, not as one blob.
    assert len(tokens) > 1
    assert "".join(tokens) == ANSWER

    done = events[-1][1]
    assert done["answer"] == ANSWER
    assert [c["index"] for c in done["citations"]] == [1]


def test_router_json_is_never_streamed_to_the_reader(monkeypatch):
    _fake_llm(monkeypatch, ROUTE_REPLY, ANSWER)
    tokens = "".join(d for k, d in _collect("q", _retriever(0.9)) if k == "token")
    assert "sources" not in tokens


def test_a_refusal_still_arrives_as_an_answer(monkeypatch):
    _fake_llm(monkeypatch, ROUTE_REPLY)  # only the router is called
    events = _collect("what is langchain?", _retriever(0.05))  # below the cutoff
    tokens = [d for k, d in events if k == "token"]
    assert tokens == [nodes.REFUSAL]
    assert events[-1][1]["citations"] == []


# -- the HTTP endpoints -------------------------------------------------------


def _client(monkeypatch, retriever: SourceRetriever) -> TestClient:
    # Not used as a context manager, so startup does not try to reach a real index.
    monkeypatch.setattr(main, "_retriever", retriever)
    monkeypatch.setattr(main, "_chunk_count", 1)
    monkeypatch.setattr(main, "_cache", None)
    return TestClient(main.app)


def _parse_events(body: str) -> list[tuple[str, dict]]:
    events = []
    for block in body.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        events.append((lines["event"], json.loads(lines["data"])))
    return events


def test_stream_endpoint_sends_server_sent_events(monkeypatch):
    _fake_llm(monkeypatch, ROUTE_REPLY, ANSWER)
    client = _client(monkeypatch, _retriever(0.9))
    with client.stream("POST", "/ask/stream", json={"question": "who owns Payments?"}) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        events = _parse_events(r.read().decode())

    names = [name for name, _ in events]
    assert names[0] == "status" and events[0][1]["stage"] == "started"
    assert names[-1] == "done"
    assert "".join(d["text"] for n, d in events if n == "token") == ANSWER
    assert events[-1][1]["cached"] is False


def test_ask_endpoint_returns_the_same_shape(monkeypatch):
    _fake_llm(monkeypatch, ROUTE_REPLY, ANSWER)
    client = _client(monkeypatch, _retriever(0.9))
    body = client.post("/ask", json={"question": "who owns Payments?"}).json()
    assert body["answer"] == ANSWER
    assert body["selected_sources"] == ["city_map"]
    assert [c["index"] for c in body["citations"]] == [1]


def test_a_failure_mid_stream_is_reported_inside_the_stream(monkeypatch):
    """Headers are already sent with a 200, so an error must travel as an event."""
    _fake_llm(monkeypatch, ROUTE_REPLY)

    class BrokenStore(StubStore):
        def search(self, *args, **kwargs):
            raise ConnectionError("vector store went away")

    broken = SourceRetriever(store=BrokenStore([]), embeddings=StubEmbeddings())
    client = _client(monkeypatch, broken)
    with client.stream("POST", "/ask/stream", json={"question": "who owns Payments?"}) as r:
        events = _parse_events(r.read().decode())
    assert events[-1][0] == "error"
    assert "vector store went away" in events[-1][1]["message"]
