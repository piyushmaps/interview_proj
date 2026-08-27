"""Retriever policy: relevance cutoff and source pass-through.

The backend is stubbed here on purpose — what is under test is the policy the
retriever applies to results, not how any store finds them.
"""

import pytest

from src.retrieval.retriever import SourceRetriever
from src.vectorstore.base import Chunk, SearchHit


class StubStore:
    """Returns a fixed ranking and records how it was asked."""

    def __init__(self, hits: list[SearchHit]) -> None:
        self.hits = hits
        self.calls: list[dict] = []

    def search(self, vector, k, sources=None, query_text=None):
        self.calls.append({"k": k, "sources": sources, "query_text": query_text})
        return self.hits[:k]

    def ensure_ready(self, dim): ...
    def upsert(self, chunks, vectors): return 0
    def delete_docs(self, doc_ids): return 0
    def count(self): return len(self.hits)
    def persist(self): ...


class StubEmbeddings:
    def embed_query(self, text: str) -> list[float]:
        return [1.0, 0.0]


def _hit(cid: str, score: float, source: str = "devportal") -> SearchHit:
    return SearchHit(
        chunk=Chunk(id=cid, doc_id=cid, content=f"body {cid}", source=source,
                    title=f"t {cid}", url=f"u/{cid}"),
        score=score,
    )


def _retriever(hits: list[SearchHit]) -> tuple[SourceRetriever, StubStore]:
    store = StubStore(hits)
    return SourceRetriever(store=store, embeddings=StubEmbeddings()), store


def test_distant_results_are_dropped():
    retriever, _ = _retriever([_hit("a", 0.85), _hit("b", 0.40), _hit("c", 0.09)])
    got = retriever.retrieve("q", k=5, min_similarity=0.28)
    assert [c.content for c in got] == ["body a", "body b"]


def test_off_corpus_question_yields_nothing():
    # Every neighbour is far: the honest answer is no evidence, not a page of
    # citations that support nothing.
    retriever, _ = _retriever([_hit("a", 0.09), _hit("b", 0.05)])
    assert retriever.retrieve("what is langchain?", k=5, min_similarity=0.28) == []


def test_cutoff_can_be_disabled():
    retriever, _ = _retriever([_hit("a", 0.85), _hit("b", -0.10)])
    assert len(retriever.retrieve("q", k=5, min_similarity=None)) == 2


def test_configured_cutoff_is_used_by_default():
    retriever, _ = _retriever([_hit("a", 0.85), _hit("b", 0.01)])
    got = retriever.retrieve("q", k=5)
    assert [c.content for c in got] == ["body a"]


def test_routed_sources_and_query_text_reach_the_backend():
    retriever, store = _retriever([_hit("a", 0.9, "jira")])
    retriever.retrieve("who owns payments", sources=["jira", "city_map"], k=3)
    call = store.calls[0]
    assert call["sources"] == ["jira", "city_map"]
    assert call["k"] == 3
    # Passed through so hybrid-capable backends can score keywords too.
    assert call["query_text"] == "who owns payments"


def test_source_enums_are_accepted():
    from src.models.document import SourceType

    retriever, store = _retriever([_hit("a", 0.9, "jira")])
    retriever.retrieve("q", sources=[SourceType.JIRA])
    assert store.calls[0]["sources"] == ["jira"]


def test_scores_are_carried_through():
    retriever, _ = _retriever([_hit("a", 0.77)])
    assert retriever.retrieve("q")[0].score == pytest.approx(0.77)
