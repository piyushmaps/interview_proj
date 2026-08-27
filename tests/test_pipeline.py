"""Incremental ingestion: skip unchanged, re-embed changed, drop vanished.

These are the properties that make a long run survivable, so they are asserted
against the real pipeline with only the embedding API faked out.
"""

import pytest

from src.config import get_settings
from src.connectors.base import BaseConnector
from src.ingestion import pipeline
from src.models.document import Document, SourceType


class FakeEmbeddings:
    """Deterministic 2-D vectors; counts how much embedding work was done."""

    def __init__(self, **_: object) -> None:
        self.calls = 0
        self.texts = 0

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        self.texts += len(texts)
        return [[float(len(t) % 7), 1.0] for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return [float(len(text) % 7), 1.0]


class ListConnector(BaseConnector):
    def __init__(self, docs: list[Document]) -> None:
        self.docs = docs

    def iter_documents(self):
        yield from self.docs


def _doc(doc_id: str, body: str) -> Document:
    return Document(
        id=doc_id,
        source=SourceType.DEVPORTAL,
        title=f"title {doc_id}",
        body=body,
        url=f"https://example.com/{doc_id}",
    )


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated store + manifest, with the embedding API faked out."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("VECTOR_STORE", "faiss")
    monkeypatch.setenv("FAISS_INDEX_PATH", str(tmp_path / "index"))
    monkeypatch.setenv("MANIFEST_PATH", str(tmp_path / "manifest.sqlite"))
    monkeypatch.setenv("EMBED_BATCH_SIZE", "8")
    get_settings.cache_clear()

    fake = FakeEmbeddings()
    monkeypatch.setattr(pipeline, "OpenAIEmbeddings", lambda **kw: fake)
    yield fake
    get_settings.cache_clear()


def _run(docs: list[Document], **kwargs):
    return pipeline.run_ingestion(connectors=[ListConnector(docs)], **kwargs)


def test_first_run_indexes_everything(env):
    stats = _run([_doc("a", "alpha"), _doc("b", "beta")])
    assert stats.documents_indexed == 2
    assert stats.documents_skipped == 0
    assert stats.chunks_indexed == 2


def test_second_run_embeds_nothing_when_nothing_changed(env):
    docs = [_doc("a", "alpha"), _doc("b", "beta")]
    _run(docs)
    embedded_after_first = env.texts

    stats = _run(docs)
    assert stats.documents_skipped == 2
    assert stats.documents_indexed == 0
    # The point of the manifest: a no-op run costs zero embedding calls.
    assert env.texts == embedded_after_first


def test_changed_document_is_re_embedded(env):
    _run([_doc("a", "alpha"), _doc("b", "beta")])
    stats = _run([_doc("a", "alpha CHANGED"), _doc("b", "beta")])
    assert stats.documents_indexed == 1
    assert stats.documents_skipped == 1


def test_full_refresh_ignores_the_manifest(env):
    docs = [_doc("a", "alpha"), _doc("b", "beta")]
    _run(docs)
    stats = _run(docs, full_refresh=True)
    assert stats.documents_indexed == 2
    assert stats.documents_skipped == 0


def test_documents_that_vanish_are_removed(env):
    _run([_doc("a", "alpha"), _doc("b", "beta")])
    stats = _run([_doc("a", "alpha")])
    assert stats.documents_deleted == 1

    store = pipeline.get_vector_store(get_settings())
    assert {c.doc_id for c in store.chunks.values()} == {"a"}


def test_no_prune_keeps_vanished_documents(env):
    _run([_doc("a", "alpha"), _doc("b", "beta")])
    stats = _run([_doc("a", "alpha")], prune=False)
    assert stats.documents_deleted == 0


def test_a_shrunk_document_leaves_no_orphan_chunks(env):
    long_body = "para one.\n\n" + ("filler sentence. " * 200)
    _run([_doc("a", long_body)])
    store = pipeline.get_vector_store(get_settings())
    assert store.count() > 1

    _run([_doc("a", "now short")])
    reopened = pipeline.get_vector_store(get_settings())
    assert reopened.count() == 1


def test_interrupted_run_resumes_from_where_it_stopped(env, monkeypatch):
    docs = [_doc(f"d{i}", f"body {i}") for i in range(20)]

    # Fail partway through, as a rate limit or a dropped connection would.
    real_embed = env.embed_documents
    def explode_on_third(texts):
        if env.calls >= 2:
            raise RuntimeError("simulated API outage")
        return real_embed(texts)
    monkeypatch.setattr(env, "embed_documents", explode_on_third)
    monkeypatch.setattr(pipeline, "EMBED_MAX_ATTEMPTS", 1)

    with pytest.raises(RuntimeError):
        _run(docs)

    # Resume: the batches that landed are remembered, and only the rest is done.
    monkeypatch.setattr(env, "embed_documents", real_embed)
    stats = _run(docs)
    assert stats.documents_skipped == 16
    assert stats.documents_indexed == 4
