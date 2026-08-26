"""Vector retrieval with optional source filters."""

from __future__ import annotations

from dataclasses import dataclass

from langchain_community.vectorstores import FAISS
from langchain_core.documents import Document as LCDocument

from src.config import get_settings
from src.ingestion.pipeline import load_index
from src.models.document import SourceType

# Growth factor applied to the fetch window on each round when filtering.
FETCH_ESCALATION = 3


@dataclass
class RetrievedChunk:
    content: str
    source: str
    title: str
    url: str
    score: float | None = None


class SourceRetriever:
    def __init__(self, store: FAISS | None = None) -> None:
        settings = get_settings()
        self.store = store or load_index()
        self.top_k = settings.top_k
        self.relevance_threshold = settings.relevance_threshold

    def retrieve(
        self,
        query: str,
        sources: list[SourceType] | list[str] | None = None,
        k: int | None = None,
        max_distance: float | None = -1.0,
    ) -> list[RetrievedChunk]:
        k = k or self.top_k
        # -1.0 sentinel = use the configured default; None = no cutoff.
        if max_distance == -1.0:
            max_distance = self.relevance_threshold
        source_values = None
        if sources:
            source_values = {
                s.value if isinstance(s, SourceType) else s for s in sources
            }

        if not source_values:
            results = self.store.similarity_search_with_score(query, k=k)
            return _drop_irrelevant(
                [_to_chunk(doc, float(score)) for doc, score in results], max_distance
            )

        # FAISS has no native metadata filter, so we over-fetch and post-filter.
        # A *fixed* window silently truncates recall: when the routed sources are
        # a small slice of the corpus their chunks fall outside the global top-N
        # and disappear, so narrowing the route makes retrieval return *less*
        # — the opposite of what routing is for. Escalate the window until we
        # have k hits or the index is exhausted.
        total = self.store.index.ntotal
        fetch_k = min(k * FETCH_ESCALATION, total)
        while True:
            results = self.store.similarity_search_with_score(query, k=fetch_k)
            chunks = [
                _to_chunk(doc, float(score))
                for doc, score in results
                if doc.metadata.get("source", "") in source_values
            ][:k]
            if len(chunks) >= k or fetch_k >= total:
                # Cutoff is applied *after* escalation: widening the window only
                # ever surfaces more distant chunks, so it can never recover one
                # that failed the threshold.
                return _drop_irrelevant(chunks, max_distance)
            fetch_k = min(fetch_k * FETCH_ESCALATION, total)


def _drop_irrelevant(
    chunks: list[RetrievedChunk], max_distance: float | None
) -> list[RetrievedChunk]:
    """Discard chunks the index returned as "nearest" that are not actually close.

    FAISS always returns k neighbours, however distant. Without this, an
    off-corpus question still produces a full set of citations pointing at
    unrelated documents. Returning nothing lets the agent say so honestly.
    """
    if max_distance is None:
        return chunks
    return [c for c in chunks if c.score is None or c.score <= max_distance]


def _to_chunk(doc: LCDocument, score: float) -> RetrievedChunk:
    return RetrievedChunk(
        content=doc.page_content,
        source=str(doc.metadata.get("source", "")),
        title=str(doc.metadata.get("title", "")),
        url=str(doc.metadata.get("url", "")),
        score=score,
    )
