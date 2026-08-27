"""Pluggable vector stores: FAISS for local runs, OpenSearch for scale."""

from src.vectorstore.base import Chunk, SearchHit, VectorStore
from src.vectorstore.factory import get_vector_store

__all__ = ["Chunk", "SearchHit", "VectorStore", "get_vector_store"]
