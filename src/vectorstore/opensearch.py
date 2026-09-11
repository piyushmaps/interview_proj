"""OpenSearch backend — the one built for scale.

Three things it does that FAISS cannot, and that stop being optional once the
corpus is large:

* **Filters inside the query.** Restricting to a source is part of the kNN
  search, not a pass over its results, so a narrowly-routed question stays cheap
  no matter how small a slice of the corpus that source is.
* **Deletes and updates by document.** Re-ingesting one changed file replaces
  its chunks; nothing else is touched and nothing is rebuilt.
* **Keyword search alongside vectors.** Pure semantic search misses exact
  tokens — ticket keys, error strings, column names. Hybrid retrieval below
  fuses both rankings.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Iterable, Sequence
from typing import Any

from opensearchpy import OpenSearch
from opensearchpy.helpers import bulk

from src.vectorstore.base import Chunk, SearchHit

logger = logging.getLogger(__name__)

# Rank at which a hybrid result's contribution is halved. 60 is the value from
# the original reciprocal-rank-fusion paper and behaves well without tuning.
RRF_K = 60


class OpenSearchVectorStore:
    def __init__(
        self,
        client: OpenSearch,
        index: str,
        *,
        ef_search: int = 100,
        hybrid: bool = True,
    ) -> None:
        self.client = client
        self.index = index
        self.ef_search = ef_search
        self.hybrid = hybrid

    # -- schema ---------------------------------------------------------------

    def ensure_ready(self, dim: int) -> None:
        if self.client.indices.exists(index=self.index):
            return
        self.client.indices.create(
            index=self.index,
            body={
                "settings": {
                    "index": {"knn": True, "knn.algo_param.ef_search": self.ef_search}
                },
                "mappings": {
                    "properties": {
                        # Vectors are L2-normalised before indexing, so cosine
                        # similarity is the meaningful distance here.
                        "embedding": {
                            "type": "knn_vector",
                            "dimension": dim,
                            "method": {
                                "name": "hnsw",
                                "space_type": "cosinesimil",
                                "engine": "lucene",
                            },
                        },
                        "content": {"type": "text"},
                        "doc_id": {"type": "keyword"},
                        "source": {"type": "keyword"},
                        "title": {"type": "text"},
                        "url": {"type": "keyword"},
                        "metadata": {"type": "object", "enabled": False},
                    }
                },
            },
        )
        logger.info("created OpenSearch index %s (dim=%d)", self.index, dim)

    # -- writes ---------------------------------------------------------------

    def upsert(self, chunks: Sequence[Chunk], vectors: Sequence[Sequence[float]]) -> int:
        actions = [
            {
                "_op_type": "index",  # index-by-id is an upsert
                "_index": self.index,
                "_id": chunk.id,
                "_source": {
                    "embedding": list(vector),
                    "content": chunk.content,
                    "doc_id": chunk.doc_id,
                    "source": chunk.source,
                    "title": chunk.title,
                    "url": chunk.url,
                    "metadata": chunk.metadata,
                },
            }
            for chunk, vector in zip(chunks, vectors, strict=True)
        ]
        written, errors = bulk(self.client, actions, refresh=False, raise_on_error=False)
        if errors:
            logger.warning("%d chunks failed to index; first: %s", len(errors), errors[0])
        return written

    def delete_docs(self, doc_ids: Iterable[str]) -> int:
        ids = list(doc_ids)
        if not ids:
            return 0
        result = self.client.delete_by_query(
            index=self.index,
            body={"query": {"terms": {"doc_id": ids}}},
            refresh=True,
            conflicts="proceed",
        )
        return int(result.get("deleted", 0))

    # -- reads ----------------------------------------------------------------

    def search(
        self,
        vector: Sequence[float],
        k: int,
        sources: Iterable[str] | None = None,
        query_text: str | None = None,
    ) -> list[SearchHit]:
        source_list = list(sources) if sources else None
        knn_hits = self._knn(vector, k, source_list)
        if not (self.hybrid and query_text):
            return knn_hits
        return _fuse(knn_hits, self._bm25(query_text, k, source_list, vector), k)

    def _filter(self, sources: list[str] | None) -> dict[str, Any] | None:
        return {"terms": {"source": sources}} if sources else None

    def _knn(self, vector, k, sources) -> list[SearchHit]:
        knn: dict[str, Any] = {"vector": list(vector), "k": k}
        source_filter = self._filter(sources)
        if source_filter:
            knn["filter"] = source_filter
        # The similarity score comes back from OpenSearch, so the stored vector
        # is never needed here. Returning it anyway made a six-result response
        # about 190 KB instead of about 5 KB — measured — on every search.
        body = {
            "size": k,
            "query": {"knn": {"embedding": knn}},
            "_source": {"excludes": ["embedding"]},
        }
        hits = self.client.search(index=self.index, body=body)["hits"]["hits"]
        return [SearchHit(chunk=_to_chunk(h), score=_cosine_from_score(h["_score"])) for h in hits]

    def _bm25(self, query_text: str, k: int, sources, vector: Sequence[float]) -> list[SearchHit]:
        query: dict[str, Any] = {
            "bool": {
                "must": [
                    {"multi_match": {"query": query_text, "fields": ["content", "title^2"]}}
                ]
            }
        }
        source_filter = self._filter(sources)
        if source_filter:
            query["bool"]["filter"] = source_filter

        # The stored vector comes back with each hit so its cosine similarity can
        # be computed here. BM25 scores are unbounded and not comparable to
        # cosine, so a keyword-only hit carrying its BM25 score would sail past
        # any relevance cutoff downstream — an off-corpus question would come
        # back "answered" purely because some chunk shared a word with it.
        # BM25 still decides this list's *rank*; only the score is restated.
        hits = self.client.search(
            index=self.index,
            body={"size": k, "query": query, "_source": {"excludes": []}},
        )
        return [
            SearchHit(chunk=_to_chunk(h), score=_cosine(vector, h["_source"].get("embedding")))
            for h in hits["hits"]["hits"]
        ]

    def count(self) -> int:
        if not self.client.indices.exists(index=self.index):
            return 0
        return int(self.client.count(index=self.index)["count"])

    def persist(self) -> None:
        if self.client.indices.exists(index=self.index):
            self.client.indices.refresh(index=self.index)


def _cosine_from_score(score: float) -> float:
    """Undo OpenSearch's cosinesimil scaling, which maps cosine into [0, 1].

    Verified against a live cluster rather than trusted from documentation —
    see tests/test_opensearch_store.py::test_score_is_cosine_similarity.
    """
    return 2.0 * float(score) - 1.0


def _cosine(query: Sequence[float], stored: Sequence[float] | None) -> float:
    """Cosine similarity computed here, for hits whose score is not already one."""
    if not stored:
        return 0.0
    dot = sum(a * b for a, b in zip(query, stored, strict=False))
    norm = math.sqrt(sum(a * a for a in query)) * math.sqrt(sum(b * b for b in stored))
    return dot / norm if norm else 0.0


def _to_chunk(hit: dict[str, Any]) -> Chunk:
    src = hit["_source"]
    return Chunk(
        id=hit["_id"],
        doc_id=src.get("doc_id", ""),
        content=src.get("content", ""),
        source=src.get("source", ""),
        title=src.get("title", ""),
        url=src.get("url", ""),
        metadata=src.get("metadata") or {},
    )


def _fuse(primary: list[SearchHit], secondary: list[SearchHit], k: int) -> list[SearchHit]:
    """Reciprocal-rank fusion of the vector and keyword rankings.

    Fusing on *rank* rather than score sidesteps the fact that BM25 and cosine
    live on incomparable scales — there are no normalisation constants to tune,
    and a chunk both rankings like beats one that a single ranking loves.

    Both inputs carry cosine scores (see `_bm25`), so the surviving score means
    the same thing whichever ranking a chunk arrived on, and relevance cutoffs
    downstream keep working.
    """
    contribution: dict[str, float] = {}
    best: dict[str, SearchHit] = {}
    for ranking in (primary, secondary):
        for rank, hit in enumerate(ranking, start=1):
            cid = hit.chunk.id
            contribution[cid] = contribution.get(cid, 0.0) + 1.0 / (RRF_K + rank)
            best.setdefault(cid, hit)
    ordered = sorted(contribution, key=lambda cid: contribution[cid], reverse=True)
    return [best[cid] for cid in ordered[:k]]
