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
        return _fuse(knn_hits, self._bm25(query_text, k, source_list), k)

    def _filter(self, sources: list[str] | None) -> dict[str, Any] | None:
        return {"terms": {"source": sources}} if sources else None

    def _knn(self, vector, k, sources) -> list[SearchHit]:
        knn: dict[str, Any] = {"vector": list(vector), "k": k}
        source_filter = self._filter(sources)
        if source_filter:
            knn["filter"] = source_filter
        body = {"size": k, "query": {"knn": {"embedding": knn}}}
        hits = self.client.search(index=self.index, body=body)["hits"]["hits"]
        return [SearchHit(chunk=_to_chunk(h), score=_cosine_from_score(h["_score"])) for h in hits]

    def _bm25(self, query_text: str, k: int, sources) -> list[SearchHit]:
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
        hits = self.client.search(index=self.index, body={"size": k, "query": query})
        # BM25 scores are unbounded and not comparable to cosine, so they are
        # used only for their *ranking* during fusion.
        return [SearchHit(chunk=_to_chunk(h), score=float(h["_score"])) for h in hits["hits"]["hits"]]

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
    and a chunk both rankings like beats one that a single ranking loves. The
    surviving score stays the vector similarity, so relevance cutoffs downstream
    keep their meaning.
    """
    contribution: dict[str, float] = {}
    best: dict[str, SearchHit] = {}
    for ranking in (primary, secondary):
        is_primary = ranking is primary
        for rank, hit in enumerate(ranking, start=1):
            cid = hit.chunk.id
            contribution[cid] = contribution.get(cid, 0.0) + 1.0 / (RRF_K + rank)
            if cid not in best or is_primary:
                best[cid] = hit
    ordered = sorted(contribution, key=lambda cid: contribution[cid], reverse=True)
    return [best[cid] for cid in ordered[:k]]
