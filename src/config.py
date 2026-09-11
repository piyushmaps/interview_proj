"""Application settings loaded from environment / .env."""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT_DIR / "data"
SAMPLE_DIR = DATA_DIR / "sample"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    # Model that scores groundedness. Blank falls back to openai_model; a
    # stronger judge than the generator is often worth the cost.
    judge_model: str = ""
    openai_embedding_model: str = "text-embedding-3-small"
    groq_api_key: str = ""

    # --- tracing (Langfuse) ---
    # Absent keys mean tracing is simply off; nothing else changes.
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    # Region-specific. Either name works; LANGFUSE_BASE_URL wins if both are set.
    langfuse_host: str = ""
    langfuse_base_url: str = ""

    # Slack connector
    slack_user_token: str = ""

    # Jira connector
    jira_api_token: str = ""
    jira_instance_url: str = ""
    jira_username: str = ""
    jira_cloud: bool = True

    vector_store: str = "faiss"  # faiss | opensearch
    faiss_index_path: str = "data/index/faiss"

    opensearch_host: str = "localhost"
    opensearch_port: int = 9200
    opensearch_index: str = "capability_docs"
    opensearch_use_ssl: bool = False
    opensearch_verify_certs: bool = False
    opensearch_user: str = ""
    opensearch_password: str = ""
    opensearch_timeout: int = 60
    # HNSW breadth at query time: higher is more accurate and slower.
    opensearch_ef_search: int = 100
    # Connections kept open to OpenSearch. The client default keeps one, so
    # every simultaneous search beyond the first opened a fresh connection
    # and threw it away afterwards.
    opensearch_pool_size: int = 32

    # Fuse keyword (BM25) and vector rankings. Pure semantic search misses exact
    # tokens — ticket keys, error strings, column names. OpenSearch only.
    hybrid_search: bool = True

    aws_region: str = "us-east-1"
    s3_bucket: str = "intuit-agentic-rag-docs"

    app_host: str = "0.0.0.0"
    app_port: int = 8000
    log_level: str = "INFO"

    # --- answer cache ---
    # The same questions repeat constantly in an internal assistant; a hit
    # costs nothing where a miss costs two LLM calls and ~3 seconds.
    cache_enabled: bool = True
    cache_max_entries: int = 2000
    # Keep below the ingestion interval so staleness is bounded by design.
    cache_ttl_seconds: float = 900.0
    # Cosine above which two questions are treated as the same question.
    # Measured, not guessed: rephrasings of one question score 0.82-0.87, while
    # questions about different capabilities top out at 0.65. A wrong hit is a
    # wrong answer, so the threshold sits above that ceiling, not near it.
    cache_similarity: float = 0.75

    # Retrieval defaults
    top_k: int = 6
    # Max L2 distance for a chunk to count as relevant. Measured on the sample
    # corpus: on-topic questions score 0.54-0.79, off-topic ones 1.35-1.77.
    # Minimum cosine similarity for a chunk to count as relevant. Measured on
    # the sample corpus: on-topic questions score 0.69-0.85, off-topic 0.09 and
    # below. Scores are cosine everywhere, so this means the same in any backend.
    min_similarity: float = 0.28

    # --- ingestion ---
    # Chunks per embedding round-trip. Small enough that a failure costs one
    # batch, large enough to keep request overhead low.
    embed_batch_size: int = 256
    # Flush the index and manifest every N batches, so a long run is resumable.
    checkpoint_every_batches: int = 20
    # Tracks content hashes so unchanged documents are never re-embedded.
    manifest_path: str = "data/index/manifest.sqlite"
    # Files that fail extraction are logged and skipped; abort past this many.
    max_extraction_failures: int = 100

    # Sheets at or below this stay one-document-per-row (reference tables such
    # as an ownership roster). Larger ones are profiled instead of enumerated.
    csv_small_sheet_rows: int = 500
    # For large sheets that carry prose, how many rows to index individually.
    csv_max_text_rows: int = 2000
    # Pages per PDF document. Bounds memory and makes citations page-specific.
    pdf_pages_per_document: int = 50
    chunk_size: int = 800
    chunk_overlap: int = 120


    @property
    def langfuse_url(self) -> str:
        return self.langfuse_base_url or self.langfuse_host or "https://cloud.langfuse.com"

    @property
    def tracing_enabled(self) -> bool:
        return bool(self.langfuse_public_key and self.langfuse_secret_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()
