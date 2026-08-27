"""FAISS backend — the local one.

Kept because it needs no server: clone the repo, run ingestion, ask a question.
It is honest about being the small-scale option. Two limits are structural, not
bugs to fix here:

* **No metadata filtering.** Source filters are applied after the search, with
  an escalating window to stop narrow routes from silently losing results. That
  workaround lives here rather than in the retriever, so callers never inherit
  a limitation of one backend.
* **Everything is resident.** A flat index scans every vector per query and
  holds them all in memory: fine for the ~40k chunks of a laptop corpus, a wall
  at tens of millions. That is the point at which OpenSearch takes over.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Sequence
from dataclasses import asdict
from pathlib import Path

import faiss
import numpy as np

from src.vectorstore.base import Chunk, SearchHit

logger = logging.getLogger(__name__)

# Growth factor for the post-filter fetch window (see `search`).
FETCH_ESCALATION = 3


class FaissVectorStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.index: faiss.Index | None = None
        self.chunks: dict[int, Chunk] = {}
        self.by_chunk_id: dict[str, int] = {}
        self._next_id = 0
        self._load()

    # -- lifecycle ------------------------------------------------------------

    def ensure_ready(self, dim: int) -> None:
        if self.index is None:
            # Inner product over L2-normalised vectors *is* cosine similarity,
            # so scores need no conversion. IDMap2 adds delete-by-id.
            self.index = faiss.IndexIDMap2(faiss.IndexFlatIP(dim))
            logger.info("created FAISS index (dim=%d)", dim)

    def _load(self) -> None:
        vec_file, meta_file = self.path / "index.faiss", self.path / "chunks.json"
        if not (vec_file.exists() and meta_file.exists()):
            return
        self.index = faiss.read_index(str(vec_file))
        raw = json.loads(meta_file.read_text(encoding="utf-8"))
        self.chunks = {int(k): Chunk(**v) for k, v in raw["chunks"].items()}
        self.by_chunk_id = {c.id: i for i, c in self.chunks.items()}
        self._next_id = int(raw.get("next_id", len(self.chunks)))
        logger.info("loaded FAISS index: %d chunks", len(self.chunks))

    def persist(self) -> None:
        if self.index is None:
            return
        self.path.mkdir(parents=True, exist_ok=True)
        faiss.write_index(self.index, str(self.path / "index.faiss"))
        (self.path / "chunks.json").write_text(
            json.dumps(
                {
                    "next_id": self._next_id,
                    "chunks": {str(i): asdict(c) for i, c in self.chunks.items()},
                }
            ),
            encoding="utf-8",
        )

    # -- writes ---------------------------------------------------------------

    def upsert(self, chunks: Sequence[Chunk], vectors: Sequence[Sequence[float]]) -> int:
        if not chunks:
            return 0
        self.ensure_ready(len(vectors[0]))
        assert self.index is not None

        # Re-writing a chunk id replaces it, matching OpenSearch's semantics.
        replaced = [self.by_chunk_id[c.id] for c in chunks if c.id in self.by_chunk_id]
        if replaced:
            self._remove(replaced)

        ids = np.arange(self._next_id, self._next_id + len(chunks), dtype=np.int64)
        self._next_id += len(chunks)
        self.index.add_with_ids(_normalize(vectors), ids)
        for numeric_id, chunk in zip(ids.tolist(), chunks, strict=True):
            self.chunks[numeric_id] = chunk
            self.by_chunk_id[chunk.id] = numeric_id
        return len(chunks)

    def delete_docs(self, doc_ids: Iterable[str]) -> int:
        targets = set(doc_ids)
        if not targets:
            return 0
        victims = [i for i, c in self.chunks.items() if c.doc_id in targets]
        self._remove(victims)
        return len(victims)

    def _remove(self, numeric_ids: list[int]) -> None:
        if not numeric_ids or self.index is None:
            return
        self.index.remove_ids(np.array(numeric_ids, dtype=np.int64))
        for i in numeric_ids:
            chunk = self.chunks.pop(i, None)
            if chunk is not None:
                self.by_chunk_id.pop(chunk.id, None)

    # -- reads ----------------------------------------------------------------

    def search(
        self,
        vector: Sequence[float],
        k: int,
        sources: Iterable[str] | None = None,
        query_text: str | None = None,
    ) -> list[SearchHit]:
        del query_text  # vector-only backend; hybrid needs OpenSearch
        if self.index is None or self.index.ntotal == 0:
            return []
        wanted = set(sources) if sources else None
        query = _normalize([vector])

        if wanted is None:
            return self._top(query, k, None)

        # No native filter: fetch wider than k and keep matching chunks. A fixed
        # window would silently drop results when the routed sources are a small
        # slice of the corpus, so it grows until k are found or the index runs out.
        fetch = min(k * FETCH_ESCALATION, self.index.ntotal)
        while True:
            hits = self._top(query, fetch, wanted)[:k]
            if len(hits) >= k or fetch >= self.index.ntotal:
                return hits
            fetch = min(fetch * FETCH_ESCALATION, self.index.ntotal)

    def _top(self, query: np.ndarray, k: int, wanted: set[str] | None) -> list[SearchHit]:
        assert self.index is not None
        scores, ids = self.index.search(query, min(k, self.index.ntotal))
        hits: list[SearchHit] = []
        for score, numeric_id in zip(scores[0].tolist(), ids[0].tolist(), strict=True):
            chunk = self.chunks.get(numeric_id)
            if chunk is None or (wanted is not None and chunk.source not in wanted):
                continue
            hits.append(SearchHit(chunk=chunk, score=float(score)))
        return hits

    def count(self) -> int:
        return len(self.chunks)


def _normalize(vectors: Sequence[Sequence[float]]) -> np.ndarray:
    """L2-normalise so inner product equals cosine similarity."""
    array = np.asarray(vectors, dtype=np.float32)
    faiss.normalize_L2(array)
    return array
