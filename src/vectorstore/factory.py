"""Pick a backend from configuration.

`VECTOR_STORE=faiss` for a laptop, `opensearch` for anything real. Nothing
outside this module knows which one is in use.
"""

from __future__ import annotations

import logging

from src.config import ROOT_DIR, Settings, get_settings
from src.vectorstore.base import VectorStore

logger = logging.getLogger(__name__)


def get_vector_store(settings: Settings | None = None) -> VectorStore:
    settings = settings or get_settings()
    backend = settings.vector_store.strip().lower()

    if backend == "opensearch":
        return _opensearch(settings)
    if backend == "faiss":
        from src.vectorstore.faiss_store import FaissVectorStore

        return FaissVectorStore(ROOT_DIR / settings.faiss_index_path)
    raise ValueError(f"Unknown VECTOR_STORE={backend!r}; expected 'faiss' or 'opensearch'.")


def _opensearch(settings: Settings) -> VectorStore:
    from opensearchpy import OpenSearch

    from src.vectorstore.opensearch import OpenSearchVectorStore

    auth = (
        (settings.opensearch_user, settings.opensearch_password)
        if settings.opensearch_user
        else None
    )
    client = OpenSearch(
        hosts=[{"host": settings.opensearch_host, "port": settings.opensearch_port}],
        http_auth=auth,
        use_ssl=settings.opensearch_use_ssl,
        verify_certs=settings.opensearch_verify_certs,
        ssl_show_warn=False,
        timeout=settings.opensearch_timeout,
        max_retries=3,
        retry_on_timeout=True,
    )
    return OpenSearchVectorStore(
        client,
        settings.opensearch_index,
        ef_search=settings.opensearch_ef_search,
        hybrid=settings.hybrid_search,
    )
