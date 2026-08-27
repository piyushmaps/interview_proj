"""OpenSearch backend, against a live cluster.

Skipped when nothing is listening on the configured host, so the suite still
runs on a laptop with no Docker. Start one with:

    docker compose -f infra/docker-compose.yml up -d opensearch

The first test matters most: it verifies the score conversion empirically
rather than trusting the documented scaling, because every relevance cutoff
downstream assumes scores are cosine similarity.
"""

from __future__ import annotations

import math
import uuid

import pytest

from src.config import get_settings
from src.vectorstore.base import Chunk

opensearchpy = pytest.importorskip("opensearchpy")
from src.vectorstore.opensearch import OpenSearchVectorStore  # noqa: E402

QUERY = [1.0, 0.0]


def _unit(angle_deg: float) -> list[float]:
    r = math.radians(angle_deg)
    return [math.cos(r), math.sin(r)]


def _chunk(cid: str, source: str, doc_id: str | None = None, content: str | None = None) -> Chunk:
    return Chunk(
        id=cid,
        doc_id=doc_id or cid.split("#")[0],
        content=content or f"content of {cid}",
        source=source,
        title=f"title {cid}",
        url=f"https://example.com/{cid}",
    )


@pytest.fixture(scope="module")
def client():
    settings = get_settings()
    c = opensearchpy.OpenSearch(
        hosts=[{"host": settings.opensearch_host, "port": settings.opensearch_port}],
        use_ssl=settings.opensearch_use_ssl,
        verify_certs=False,
        ssl_show_warn=False,
        timeout=5,
    )
    try:
        c.cluster.health()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no OpenSearch at {settings.opensearch_host}: {exc}")
    return c


@pytest.fixture
def store(client):
    """A throwaway index per test, deleted afterwards."""
    index = f"test-{uuid.uuid4().hex[:10]}"
    s = OpenSearchVectorStore(client, index)
    s.ensure_ready(2)
    yield s
    client.indices.delete(index=index, ignore=[404])


def _write(store: OpenSearchVectorStore, chunks, vectors) -> None:
    store.upsert(chunks, vectors)
    store.persist()  # refresh so writes are searchable immediately


def test_score_is_cosine_similarity(store):
    """The claim `_cosine_from_score` makes, checked against the real engine."""
    _write(store, [_chunk("a", "jira"), _chunk("b", "jira"), _chunk("c", "jira")],
           [_unit(0), _unit(60), _unit(90)])

    scores = {h.chunk.id: h.score for h in store.search(QUERY, k=3)}
    assert scores["a"] == pytest.approx(1.0, abs=0.01)   # identical direction
    assert scores["b"] == pytest.approx(0.5, abs=0.01)   # cos 60 degrees
    assert scores["c"] == pytest.approx(0.0, abs=0.01)   # orthogonal


def test_filter_is_applied_inside_the_query(store):
    """The reason OpenSearch exists here: no over-fetching, no post-filtering."""
    chunks, vectors = [], []
    for i in range(30):
        # The only devportal chunks are the two furthest from the query.
        chunks.append(_chunk(f"c{i}", "devportal" if i in (28, 29) else "city_map"))
        vectors.append(_unit(i))
    _write(store, chunks, vectors)

    hits = store.search(QUERY, k=2, sources=["devportal"])
    assert {h.chunk.id for h in hits} == {"c28", "c29"}


def test_reindexing_a_chunk_id_replaces_it(store):
    _write(store, [_chunk("a", "jira")], [_unit(0)])
    _write(store, [_chunk("a", "jira")], [_unit(90)])
    assert store.count() == 1
    assert store.search(QUERY, k=5)[0].score == pytest.approx(0.0, abs=0.01)


def test_delete_removes_every_chunk_of_a_document(store):
    _write(store,
           [_chunk("d1#0", "jira", "d1"), _chunk("d1#1", "jira", "d1"), _chunk("d2#0", "jira", "d2")],
           [_unit(0), _unit(10), _unit(20)])
    assert store.delete_docs(["d1"]) == 2
    assert store.count() == 1


def test_hybrid_search_surfaces_an_exact_keyword_match(store):
    """Pure vector search misses rare literal tokens. The keyword half of the
    hybrid ranking is what brings a ticket key or error string back."""
    _write(
        store,
        [
            _chunk("near", "jira", content="general notes about payment processing"),
            _chunk("exact", "jira", content="ticket CAP-120 covers the Auth Gateway migration"),
        ],
        # The keyword-bearing chunk is the *worse* vector match on purpose.
        [_unit(5), _unit(85)],
    )

    vector_only = OpenSearchVectorStore(store.client, store.index, hybrid=False)
    assert vector_only.search(QUERY, k=1)[0].chunk.id == "near"

    hybrid = store.search(QUERY, k=2, query_text="CAP-120")
    assert "exact" in {h.chunk.id for h in hybrid}


def test_empty_index_returns_nothing(store):
    assert store.search(QUERY, k=5) == []
