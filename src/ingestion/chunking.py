"""Source-aware chunking."""

from langchain_core.documents import Document as LCDocument
from langchain_text_splitters import RecursiveCharacterTextSplitter

from src.config import get_settings
from src.models.document import Document, SourceType


def to_langchain_chunks(documents: list[Document]) -> list[LCDocument]:
    """Chunk docs; keep Slack/Jira/city-map as single chunks when short."""
    settings = get_settings()
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=settings.chunk_size,
        chunk_overlap=settings.chunk_overlap,
        separators=["\n## ", "\n# ", "\n\n", "\n", " "],
    )

    chunks: list[LCDocument] = []
    for doc in documents:
        meta = {
            "doc_id": doc.id,
            "source": doc.source.value,
            "title": doc.title,
            "url": doc.url,
            "author": doc.author or "",
            **{k: v for k, v in doc.metadata.items() if isinstance(v, (str, int, float, bool))},
        }
        text = doc.to_text()

        # Keep short operational docs whole
        if doc.source in {SourceType.SLACK, SourceType.JIRA, SourceType.CITY_MAP} and len(text) <= settings.chunk_size:
            chunks.append(LCDocument(page_content=text, metadata=meta))
            continue

        for i, piece in enumerate(splitter.split_text(text)):
            piece_meta = {**meta, "chunk_index": i}
            chunks.append(LCDocument(page_content=piece, metadata=piece_meta))
    return chunks
