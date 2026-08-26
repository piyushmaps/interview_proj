"""End-to-end ingestion: connectors → chunks → FAISS index."""

from __future__ import annotations

import logging
from pathlib import Path

from langchain_community.vectorstores import FAISS
from langchain_openai import OpenAIEmbeddings

from src.config import ROOT_DIR, get_settings
from src.connectors import (
    CityMapConnector,
    DevPortalConnector,
    JiraConnector,
    SlackConnector,
)
from src.ingestion.chunking import to_langchain_chunks
from src.models.document import Document

logger = logging.getLogger(__name__)


def load_all_documents() -> list[Document]:
    connectors = [
        JiraConnector(),
        SlackConnector(),
        DevPortalConnector(),
        CityMapConnector(),
    ]
    docs: list[Document] = []
    for connector in connectors:
        loaded = connector.load()
        logger.info("%s → %d docs", connector.__class__.__name__, len(loaded))
        docs.extend(loaded)
    return docs


def build_index(documents: list[Document] | None = None) -> FAISS:
    settings = get_settings()
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is required. Copy .env.example → .env and set it.")

    documents = documents or load_all_documents()
    chunks = to_langchain_chunks(documents)
    logger.info("Chunked into %d pieces", len(chunks))

    embeddings = OpenAIEmbeddings(
        model=settings.openai_embedding_model,
        api_key=settings.openai_api_key,
    )
    store = FAISS.from_documents(chunks, embeddings)

    index_path = ROOT_DIR / settings.faiss_index_path
    index_path.parent.mkdir(parents=True, exist_ok=True)
    store.save_local(str(index_path))
    logger.info("Saved FAISS index → %s", index_path)
    return store


def load_index() -> FAISS:
    settings = get_settings()
    index_path = ROOT_DIR / settings.faiss_index_path
    if not Path(index_path).exists():
        raise FileNotFoundError(
            f"No index at {index_path}. Run: python -m src.ingestion.pipeline"
        )
    embeddings = OpenAIEmbeddings(
        model=settings.openai_embedding_model,
        api_key=settings.openai_api_key,
    )
    return FAISS.load_local(
        str(index_path),
        embeddings,
        allow_dangerous_deserialization=True,
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    docs = load_all_documents()
    print(f"Loaded {len(docs)} documents from 4 sources")
    build_index(docs)
    print("Index ready.")


if __name__ == "__main__":
    main()
