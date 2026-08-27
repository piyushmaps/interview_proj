"""Streaming, resumable ingestion.

    connectors → documents → hash → chunk → embed → vector store
                               ↓ unchanged?
                            skipped

Every stage is a generator, so peak memory is set by the batch size, not by the
corpus. The properties that make this survive a real corpus:

* **Incremental.** Each document is fingerprinted; unchanged ones are never
  re-embedded. The second run over an unchanged corpus costs zero API calls.
* **Resumable.** Progress is committed as batches land, so a run killed at 70%
  restarts at 70%. Resume needs no extra machinery — it falls out of skipping
  unchanged documents.
* **Isolated failures.** A corrupt file is logged and skipped. A failing
  embedding API is fatal on purpose: better to stop than to quietly index a
  partial corpus, and the manifest makes the retry cheap.
* **Backend-agnostic.** Writes go through the VectorStore interface, so FAISS
  locally and OpenSearch in production run the same code path.

    python -m src.ingestion.pipeline                # incremental
    python -m src.ingestion.pipeline --full-refresh # re-embed everything
"""

from __future__ import annotations

import argparse
import logging
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field

from langchain_openai import OpenAIEmbeddings

from src.config import ROOT_DIR, Settings, get_settings
from src.connectors import (
    CityMapConnector,
    DevPortalConnector,
    JiraConnector,
    SlackConnector,
)
from src.connectors.base import BaseConnector
from src.ingestion.chunking import chunk_document
from src.ingestion.manifest import IngestionManifest, content_hash, open_manifest
from src.models.document import Document
from src.vectorstore.base import Chunk, VectorStore
from src.vectorstore.factory import get_vector_store

logger = logging.getLogger(__name__)

EMBED_MAX_ATTEMPTS = 4


def build_connectors() -> list[BaseConnector]:
    return [JiraConnector(), SlackConnector(), DevPortalConnector(), CityMapConnector()]


@dataclass
class IngestionStats:
    documents_seen: int = 0
    documents_skipped: int = 0
    documents_indexed: int = 0
    chunks_indexed: int = 0
    documents_deleted: int = 0
    batches: int = 0
    failures: list[tuple[str, str]] = field(default_factory=list)
    started: float = field(default_factory=time.monotonic)

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def render(self) -> str:
        rate = self.chunks_indexed / self.elapsed if self.elapsed else 0
        return (
            f"\n  documents seen     : {self.documents_seen:,}"
            f"\n  unchanged (skipped): {self.documents_skipped:,}"
            f"\n  indexed            : {self.documents_indexed:,}"
            f"\n  chunks written     : {self.chunks_indexed:,}"
            f"\n  documents deleted  : {self.documents_deleted:,}"
            f"\n  extraction failures: {len(self.failures):,}"
            f"\n  elapsed            : {self.elapsed:.0f}s ({rate:.0f} chunks/s)"
        )


def iter_all_documents(connectors: Sequence[BaseConnector]) -> Iterator[Document]:
    """One document stream across every source."""
    for connector in connectors:
        name = type(connector).__name__
        logger.info("reading %s", name)
        yield from connector.iter_documents()


def _embed(embeddings: OpenAIEmbeddings, texts: list[str]) -> list[list[float]]:
    """Embed one batch, retrying transient failures with exponential backoff.

    Persistent failure raises: a half-indexed corpus that reports success is
    worse than a job that stops, and the manifest makes resuming cheap.
    """
    for attempt in range(1, EMBED_MAX_ATTEMPTS + 1):
        try:
            return embeddings.embed_documents(texts)
        except Exception as exc:
            if attempt == EMBED_MAX_ATTEMPTS:
                logger.error("embedding failed after %d attempts: %s", attempt, exc)
                raise
            delay = 2**attempt
            logger.warning("embedding attempt %d failed (%s); retrying in %ds", attempt, exc, delay)
            time.sleep(delay)
    raise AssertionError("unreachable")


class _Batcher:
    """Accumulates chunks and flushes them as embed + upsert units."""

    def __init__(
        self,
        store: VectorStore,
        manifest: IngestionManifest,
        embeddings: OpenAIEmbeddings,
        settings: Settings,
        stats: IngestionStats,
    ) -> None:
        self.store = store
        self.manifest = manifest
        self.embeddings = embeddings
        self.settings = settings
        self.stats = stats
        self.chunks: list[Chunk] = []
        self.replaced: list[str] = []
        self.pending: list[tuple[str, str, str, int]] = []
        self.ready = False

    def add(self, doc: Document, digest: str, chunks: list[Chunk], is_update: bool) -> None:
        if is_update:
            # A shrunk document would otherwise leave orphaned chunks behind,
            # since ids only overwrite positions the new version still has.
            self.replaced.append(doc.id)
        self.chunks.extend(chunks)
        self.pending.append((doc.id, digest, doc.source.value, len(chunks)))
        if len(self.chunks) >= self.settings.embed_batch_size:
            self.flush()

    def flush(self) -> None:
        if not self.chunks:
            self._commit_pending()
            return

        vectors = _embed(self.embeddings, [c.content for c in self.chunks])
        if not self.ready:
            self.store.ensure_ready(len(vectors[0]))
            self.ready = True
        if self.replaced:
            self.store.delete_docs(self.replaced)
        written = self.store.upsert(self.chunks, vectors)

        self.stats.chunks_indexed += written
        self.stats.batches += 1
        logger.info(
            "batch %d — %d chunks (%d docs, %d skipped) %.0f chunks/s",
            self.stats.batches,
            self.stats.chunks_indexed,
            self.stats.documents_indexed,
            self.stats.documents_skipped,
            self.stats.chunks_indexed / max(self.stats.elapsed, 1e-9),
        )
        self.chunks.clear()
        self.replaced.clear()
        self._commit_pending()

        if self.stats.batches % self.settings.checkpoint_every_batches == 0:
            self.store.persist()
            logger.info("checkpoint saved at batch %d", self.stats.batches)

    def _commit_pending(self) -> None:
        for doc_id, digest, source, count in self.pending:
            self.manifest.record(doc_id, digest, source, count)
        self.manifest.commit()
        self.pending.clear()


def run_ingestion(
    *,
    full_refresh: bool = False,
    prune: bool = True,
    connectors: Sequence[BaseConnector] | None = None,
) -> IngestionStats:
    settings = get_settings()
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is required. Copy .env.example → .env and set it.")

    connectors = list(connectors) if connectors is not None else build_connectors()
    store = get_vector_store(settings)
    embeddings = OpenAIEmbeddings(
        model=settings.openai_embedding_model, api_key=settings.openai_api_key
    )
    stats = IngestionStats()
    seen: set[str] = set()

    with open_manifest(ROOT_DIR / settings.manifest_path) as manifest:
        batcher = _Batcher(store, manifest, embeddings, settings, stats)

        for doc in iter_all_documents(connectors):
            stats.documents_seen += 1
            seen.add(doc.id)
            digest = content_hash(doc.to_text())

            known = not full_refresh and manifest.is_unchanged(doc.id, digest)
            if known:
                stats.documents_skipped += 1
                continue

            is_update = manifest.knows(doc.id)
            chunks = chunk_document(doc)
            if not chunks:
                continue
            stats.documents_indexed += 1
            batcher.add(doc, digest, chunks, is_update)

        batcher.flush()

        # Documents that disappeared from their source: drop them so the index
        # does not answer from content that no longer exists. Only safe after a
        # complete pass, which is why a partial run must not prune.
        if prune:
            gone = manifest.vanished(seen)
            if gone:
                stats.documents_deleted = store.delete_docs(gone)
                manifest.forget(gone)
                manifest.commit()
                logger.info("pruned %d documents that vanished from source", len(gone))

        store.persist()

    for connector in connectors:
        stats.failures.extend(getattr(connector, "failures", []))
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest all sources into the vector store.")
    parser.add_argument("--full-refresh", action="store_true",
                        help="re-embed every document, ignoring content hashes")
    parser.add_argument("--no-prune", action="store_true",
                        help="keep documents that no longer exist in their source")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    settings = get_settings()
    logger.info("vector store: %s", settings.vector_store)

    stats = run_ingestion(full_refresh=args.full_refresh, prune=not args.no_prune)
    print("Ingestion complete." + stats.render())
    if stats.failures:
        print(f"\n  {len(stats.failures)} file(s) skipped:")
        for name, reason in stats.failures[:10]:
            print(f"    - {name}: {reason}")


if __name__ == "__main__":
    main()
