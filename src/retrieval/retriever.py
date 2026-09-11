"""Retrieval over whichever vector backend is configured.

This module used to carry FAISS's limitations — an escalating over-fetch window
to work around the lack of metadata filtering. That workaround now lives inside
the FAISS backend, where it belongs, and OpenSearch does not need it at all.
What is left here is the part that is genuinely retrieval policy rather than
storage mechanics: embed the query, ask for the routed sources, and drop
neighbours that are near in rank but far in meaning.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass

from langchain_openai import OpenAIEmbeddings

from src.config import get_settings
from src.models.document import SourceType
from src.vectorstore.base import VectorStore
from src.vectorstore.factory import get_vector_store


@dataclass
class RetrievedChunk:
    content: str
    source: str
    title: str
    url: str
    score: float | None = None  # cosine similarity; higher is better


class SourceRetriever:
    def __init__(
        self,
        store: VectorStore | None = None,
        embeddings: OpenAIEmbeddings | None = None,
    ) -> None:
        settings = get_settings()
        self.store = store if store is not None else get_vector_store(settings)
        self.embeddings = embeddings or OpenAIEmbeddings(
            model=settings.openai_embedding_model,
            api_key=settings.openai_api_key,
        )
        self.top_k = settings.top_k
        self.min_similarity = settings.min_similarity

    def retrieve(
        self,
        query: str,
        sources: Iterable[SourceType] | Iterable[str] | None = None,
        k: int | None = None,
        min_similarity: float | None = -2.0,
    ) -> list[RetrievedChunk]:
        """Chunks relevant to `query`, restricted to `sources` when given.

        `min_similarity` defaults to the configured cutoff; pass None to disable
        it. -2.0 is the sentinel for "use the default" because it sits below the
        cosine range, so it can never be mistaken for a real threshold.
        """
        k, min_similarity, source_values = self._resolve(k, min_similarity, sources)
        vector = self.embeddings.embed_query(query)
        hits = self.store.search(vector, k=k, sources=source_values, query_text=query)
        return self._finish(hits, min_similarity)

    async def aretrieve(
        self,
        query: str,
        sources: Iterable[SourceType] | Iterable[str] | None = None,
        k: int | None = None,
        min_similarity: float | None = -2.0,
    ) -> list[RetrievedChunk]:
        """`retrieve` for the asynchronous API path.

        The embedding call is natively asynchronous. The vector store interface
        is not, so the search runs on a worker thread — without that, each
        OpenSearch round trip would freeze every other request sharing the event
        loop. A native asynchronous OpenSearch client is the complete fix; this
        is the correct one for a synchronous interface.
        """
        k, min_similarity, source_values = self._resolve(k, min_similarity, sources)
        vector = await self.embeddings.aembed_query(query)
        hits = await asyncio.to_thread(
            self.store.search, vector, k=k, sources=source_values, query_text=query
        )
        return self._finish(hits, min_similarity)

    def _resolve(self, k, min_similarity, sources):
        k = k or self.top_k
        if min_similarity == -2.0:
            min_similarity = self.min_similarity
        source_values = (
            [s.value if isinstance(s, SourceType) else str(s) for s in sources]
            if sources
            else None
        )
        return k, min_similarity, source_values

    @staticmethod
    def _finish(hits, min_similarity: float | None) -> list[RetrievedChunk]:
        # A vector index has no notion of "no match" — it returns the k nearest
        # neighbours however distant. Without this, an off-corpus question comes
        # back with a full set of citations that support nothing.
        if min_similarity is not None:
            hits = [h for h in hits if h.score >= min_similarity]

        return [
            RetrievedChunk(
                content=h.chunk.content,
                source=h.chunk.source,
                title=h.chunk.title,
                url=h.chunk.url,
                score=h.score,
            )
            for h in hits
        ]
