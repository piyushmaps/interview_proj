"""The contract every vector backend implements.

One rule the rest of the system depends on: **score is cosine similarity, higher
is better, and it means the same thing in every backend.** FAISS natively returns
an L2 distance (lower is better) and OpenSearch returns its own scaled score;
both are converted here so callers never branch on which store is behind them.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(slots=True)
class Chunk:
    """An indexable unit of text plus everything needed to cite it.

    `id` is deterministic (`doc_id#position`) so re-ingesting the same document
    overwrites its chunks instead of duplicating them.
    """

    id: str
    doc_id: str
    content: str
    source: str
    title: str
    url: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SearchHit:
    chunk: Chunk
    score: float  # cosine similarity in [-1, 1]; higher is better


def l2_to_cosine(distance: float) -> float:
    """Convert an L2 distance between unit vectors to cosine similarity.

    Embedding APIs return normalised vectors, so |a-b|^2 = 2 - 2*cos.
    """
    return 1.0 - (distance * distance) / 2.0


@runtime_checkable
class VectorStore(Protocol):
    """Minimal surface the pipeline and retriever need.

    Deliberately small: anything a backend cannot do natively (metadata
    filtering, deletes) has to be solved by the backend, not leaked upward as a
    workaround the callers have to know about.
    """

    def ensure_ready(self, dim: int) -> None:
        """Create the index/collection if absent. Safe to call repeatedly."""

    def upsert(self, chunks: Sequence[Chunk], vectors: Sequence[Sequence[float]]) -> int:
        """Write chunks by id. Re-writing an id replaces it."""

    def delete_docs(self, doc_ids: Iterable[str]) -> int:
        """Remove every chunk belonging to these documents."""

    def search(
        self,
        vector: Sequence[float],
        k: int,
        sources: Iterable[str] | None = None,
        query_text: str | None = None,
    ) -> list[SearchHit]:
        """Nearest chunks, restricted to `sources` when given.

        `query_text` enables hybrid (keyword + vector) scoring on backends that
        support it; vector-only backends ignore it.
        """

    def count(self) -> int:
        """Number of indexed chunks."""

    def persist(self) -> None:
        """Flush to durable storage. A no-op for server-backed stores."""
