"""Regression tests for source-filtered retrieval.

The bug these lock down: with a *fixed* over-fetch window, chunks from a
narrowly-routed source that rank outside the global top-N were silently
dropped — so routing to one source returned fewer results than the index held.
"""

from langchain_core.documents import Document as LCDocument

from src.retrieval.retriever import SourceRetriever


class _StubIndex:
    def __init__(self, ntotal: int) -> None:
        self.ntotal = ntotal


class StubStore:
    """Fixed global ranking; `similarity_search_with_score` returns its first k."""

    def __init__(self, ranking: list[tuple[LCDocument, float]]) -> None:
        self.ranking = ranking
        self.index = _StubIndex(len(ranking))
        self.fetch_sizes: list[int] = []

    def similarity_search_with_score(self, query: str, k: int):
        self.fetch_sizes.append(k)
        return self.ranking[:k]


def _corpus() -> StubStore:
    """25 chunks mirroring the real index: devportal holds 4, but its last one
    sits at global rank 20 — outside the old fixed window of k*3 = 18."""
    ranking = []
    for rank in range(1, 26):
        source = "devportal" if rank in (1, 3, 4, 20) else "city_map"
        ranking.append(
            (LCDocument(page_content=f"chunk {rank}",
                        metadata={"source": source, "title": f"t{rank}", "url": f"u{rank}"}),
             float(rank) / 10),
        )
    return StubStore(ranking)


def test_narrow_filter_recovers_chunks_outside_initial_window():
    # max_distance=None isolates escalation from the relevance cutoff, which is
    # a separate concern tested below.
    store = _corpus()
    chunks = SourceRetriever(store=store).retrieve(
        "q", sources=["devportal"], k=6, max_distance=None
    )

    # All four devportal chunks, including the one at global rank 20.
    assert len(chunks) == 4
    assert {c.content for c in chunks} == {"chunk 1", "chunk 3", "chunk 4", "chunk 20"}
    # It had to widen the window to find them.
    assert len(store.fetch_sizes) > 1
    assert store.fetch_sizes[0] == 18


def test_escalation_is_bounded_by_corpus_size():
    store = _corpus()
    SourceRetriever(store=store).retrieve("q", sources=["devportal"], k=6, max_distance=None)
    assert max(store.fetch_sizes) <= store.index.ntotal


def test_filter_stops_at_k_when_source_is_plentiful():
    store = _corpus()
    chunks = SourceRetriever(store=store).retrieve("q", sources=["city_map"], k=6)
    assert len(chunks) == 6
    assert all(c.source == "city_map" for c in chunks)
    # Plentiful source: the first window suffices, no escalation.
    assert len(store.fetch_sizes) == 1


def test_unfiltered_search_fetches_exactly_k():
    store = _corpus()
    chunks = SourceRetriever(store=store).retrieve("q", k=6)
    assert len(chunks) == 6
    assert store.fetch_sizes == [6]


def _mixed_distance_corpus() -> StubStore:
    """Two close chunks, three far ones — as an off-corpus question produces."""
    ranking = [
        (LCDocument(page_content=f"chunk {i}",
                    metadata={"source": "devportal", "title": f"t{i}", "url": f"u{i}"}), dist)
        for i, dist in enumerate([0.55, 0.80, 1.35, 1.60, 1.75], start=1)
    ]
    return StubStore(ranking)


def test_distant_chunks_are_dropped_as_irrelevant():
    store = _mixed_distance_corpus()
    chunks = SourceRetriever(store=store).retrieve("q", k=5, max_distance=1.2)
    assert [c.content for c in chunks] == ["chunk 1", "chunk 2"]


def test_off_corpus_question_yields_no_evidence():
    # Every neighbour is distant: the honest result is nothing, not a full page
    # of unrelated citations.
    store = _mixed_distance_corpus()
    assert SourceRetriever(store=store).retrieve("q", k=5, max_distance=0.4) == []


def test_cutoff_can_be_disabled():
    store = _mixed_distance_corpus()
    chunks = SourceRetriever(store=store).retrieve("q", k=5, max_distance=None)
    assert len(chunks) == 5


def test_cutoff_also_applies_to_source_filtered_path():
    store = _mixed_distance_corpus()
    chunks = SourceRetriever(store=store).retrieve(
        "q", sources=["devportal"], k=5, max_distance=1.2
    )
    assert [c.content for c in chunks] == ["chunk 1", "chunk 2"]
