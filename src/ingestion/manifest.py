"""What has already been indexed, tracked by content hash.

This one table is what turns ingestion from "rebuild everything, hope it
finishes" into an incremental, resumable job:

* **Incremental.** A document whose hash is unchanged is never re-embedded.
  Re-running after editing one file costs one file, not the corpus.
* **Resumable.** Progress is recorded as batches land, so a run that dies at 70%
  resumes at 70% — resume is a consequence of idempotency, not extra machinery.
* **Deletable.** Documents that vanish from a source are detectable by
  comparing what was seen this run against what the table remembers, so the
  index does not accumulate ghosts.

SQLite rather than JSON because it stays cheap at millions of rows, commits
incrementally, and needs no server.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    doc_id       TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,
    source       TEXT NOT NULL,
    chunk_count  INTEGER NOT NULL,
    indexed_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS documents_source ON documents(source);
"""


def content_hash(text: str) -> str:
    """Stable fingerprint of a document's indexable content."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class IngestionManifest:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.executescript(_SCHEMA)
        # Durability is not worth a fsync per batch here: the manifest is
        # rebuildable by re-running ingestion, which is idempotent anyway.
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self._known: dict[str, str] = {
            row[0]: row[1] for row in self.conn.execute("SELECT doc_id, content_hash FROM documents")
        }

    def knows(self, doc_id: str) -> bool:
        """True if this document has been indexed before, at any version."""
        return doc_id in self._known

    def is_unchanged(self, doc_id: str, digest: str) -> bool:
        return self._known.get(doc_id) == digest

    def record(self, doc_id: str, digest: str, source: str, chunk_count: int) -> None:
        self.conn.execute(
            "INSERT INTO documents (doc_id, content_hash, source, chunk_count, indexed_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(doc_id) DO UPDATE SET "
            "content_hash=excluded.content_hash, source=excluded.source, "
            "chunk_count=excluded.chunk_count, indexed_at=excluded.indexed_at",
            (doc_id, digest, source, chunk_count, datetime.now(UTC).isoformat()),
        )
        self._known[doc_id] = digest

    def forget(self, doc_ids: Iterable[str]) -> None:
        ids = list(doc_ids)
        if not ids:
            return
        self.conn.executemany("DELETE FROM documents WHERE doc_id = ?", [(i,) for i in ids])
        for doc_id in ids:
            self._known.pop(doc_id, None)

    def vanished(self, seen: set[str]) -> list[str]:
        """Documents the manifest knows about that this run did not produce.

        Only meaningful after a full run; a partial run would report every
        document it had not reached yet as deleted.
        """
        return [doc_id for doc_id in self._known if doc_id not in seen]

    def commit(self) -> None:
        self.conn.commit()

    def close(self) -> None:
        self.commit()
        self.conn.close()

    def stats(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT source, COUNT(*), SUM(chunk_count) FROM documents GROUP BY source"
        ).fetchall()
        return {f"{source}": int(docs) for source, docs, _ in rows}

    def __len__(self) -> int:
        return len(self._known)


@contextmanager
def open_manifest(path: Path) -> Iterator[IngestionManifest]:
    manifest = IngestionManifest(path)
    try:
        yield manifest
    finally:
        manifest.close()
