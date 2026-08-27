"""Source-aware chunking, streamed.

Chunks are produced lazily from a document stream, so the pipeline never holds
more than the current document. Chunk ids are deterministic (`doc_id#position`),
which is what makes re-ingesting a changed document an overwrite rather than a
duplicate.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator

from langchain_text_splitters import RecursiveCharacterTextSplitter

from src.config import get_settings
from src.models.document import Document, SourceType
from src.vectorstore.base import Chunk

# Sources whose documents are conversational or record-like: splitting a
# five-message thread or a single sheet row destroys the context that makes it
# retrievable at all.
KEEP_WHOLE_SOURCES = {SourceType.SLACK, SourceType.JIRA, SourceType.CITY_MAP}


def _splitter() -> RecursiveCharacterTextSplitter:
    settings = get_settings()
    return RecursiveCharacterTextSplitter(
        chunk_size=settings.chunk_size,
        chunk_overlap=settings.chunk_overlap,
        # Prefer Markdown headings so chunks align with sections rather than
        # landing mid-sentence. HTML and PDF extraction both emit these markers.
        separators=["\n## ", "\n# ", "\n\n", "\n", " "],
    )


def iter_chunks(documents: Iterable[Document]) -> Iterator[Chunk]:
    """Yield chunks for a stream of documents, preserving citation metadata."""
    splitter = _splitter()
    keep_whole_limit = get_settings().chunk_size

    for doc in documents:
        base = {
            "doc_id": doc.id,
            "source": doc.source.value,
            "title": doc.title,
            "url": doc.url,
        }
        scalar_meta = {
            k: v for k, v in doc.metadata.items() if isinstance(v, str | int | float | bool)
        }
        text = doc.to_text()

        if doc.source in KEEP_WHOLE_SOURCES and len(text) <= keep_whole_limit:
            yield Chunk(
                id=f"{doc.id}#0",
                content=text,
                metadata={**scalar_meta, "author": doc.author or ""},
                **base,
            )
            continue

        for position, piece in enumerate(splitter.split_text(text)):
            yield Chunk(
                id=f"{doc.id}#{position}",
                content=piece,
                metadata={**scalar_meta, "author": doc.author or "", "chunk_index": position},
                **base,
            )


def chunk_document(doc: Document) -> list[Chunk]:
    """Chunks for a single document. Used where the count is needed up front."""
    return list(iter_chunks([doc]))
