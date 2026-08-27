"""FAISS backend, exercised against a real index built from crafted vectors.

Hand-made unit vectors make cosine similarity exact and need no embedding API,
so these assert on the store's actual behaviour rather than on a mock's.
"""

import math

import pytest

from src.vectorstore.base import Chunk
from src.vectorstore.faiss_store import FaissVectorStore


def _chunk(cid: str, source: str, doc_id: str | None = None) -> Chunk:
    return Chunk(
        id=cid,
        doc_id=doc_id or cid.split("#")[0],
        content=f"content of {cid}",
        source=source,
        title=f"title {cid}",
        url=f"https://example.com/{cid}",
    )


def _unit(angle_deg: float) -> list[float]:
    """A 2-D unit vector at `angle_deg` from the query direction [1, 0],
    so cosine similarity with the query is exactly cos(angle)."""
    radians = math.radians(angle_deg)
    return [math.cos(radians), math.sin(radians)]


QUERY = [1.0, 0.0]


@pytest.fixture
def store(tmp_path) -> FaissVectorStore:
    s = FaissVectorStore(tmp_path / "index")
    s.ensure_ready(2)
    return s


def test_score_is_cosine_similarity(store):
    store.upsert([_chunk("a", "jira"), _chunk("b", "jira")], [_unit(0), _unit(60)])
    hits = store.search(QUERY, k=2)
    assert [h.chunk.id for h in hits] == ["a", "b"]
    assert hits[0].score == pytest.approx(1.0, abs=1e-5)
    assert hits[1].score == pytest.approx(0.5, abs=1e-5)  # cos 60 degrees


def test_source_filter_returns_only_that_source(store):
    store.upsert(
        [_chunk("j1", "jira"), _chunk("s1", "slack"), _chunk("j2", "jira")],
        [_unit(10), _unit(5), _unit(20)],
    )
    hits = store.search(QUERY, k=5, sources=["jira"])
    assert {h.chunk.source for h in hits} == {"jira"}
    assert [h.chunk.id for h in hits] == ["j1", "j2"]


def test_narrow_filter_reaches_past_the_initial_window(store):
    """The regression this backend exists to prevent: a source that is a small
    slice of the corpus must not lose results to a fixed over-fetch window."""
    chunks, vectors = [], []
    for i in range(40):
        # Only two devportal chunks, and they are the *furthest* of the lot.
        is_target = i in (38, 39)
        chunks.append(_chunk(f"c{i}", "devportal" if is_target else "city_map"))
        vectors.append(_unit(i))
    store.upsert(chunks, vectors)

    hits = store.search(QUERY, k=2, sources=["devportal"])
    assert [h.chunk.id for h in hits] == ["c38", "c39"]


def test_reindexing_a_chunk_id_replaces_it(store):
    store.upsert([_chunk("a", "jira")], [_unit(0)])
    store.upsert([_chunk("a", "jira")], [_unit(90)])
    assert store.count() == 1
    assert store.search(QUERY, k=5)[0].score == pytest.approx(0.0, abs=1e-5)


def test_delete_removes_every_chunk_of_a_document(store):
    store.upsert(
        [_chunk("d1#0", "jira", "d1"), _chunk("d1#1", "jira", "d1"), _chunk("d2#0", "jira", "d2")],
        [_unit(0), _unit(10), _unit(20)],
    )
    assert store.delete_docs(["d1"]) == 2
    assert store.count() == 1
    assert [h.chunk.doc_id for h in store.search(QUERY, k=5)] == ["d2"]


def test_index_survives_a_restart(tmp_path):
    first = FaissVectorStore(tmp_path / "index")
    first.ensure_ready(2)
    first.upsert([_chunk("a", "jira")], [_unit(0)])
    first.persist()

    reopened = FaissVectorStore(tmp_path / "index")
    assert reopened.count() == 1
    hit = reopened.search(QUERY, k=1)[0]
    assert hit.chunk.title == "title a"
    assert hit.score == pytest.approx(1.0, abs=1e-5)


def test_empty_store_returns_nothing(store):
    assert store.search(QUERY, k=5) == []
