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
    openai_embedding_model: str = "text-embedding-3-small"
    groq_api_key: str = ""

    # LangGraph / LangSmith tracing
    langgraph_api_key: str = ""
    langsmith_api_key: str = ""

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

    aws_region: str = "us-east-1"
    s3_bucket: str = "intuit-agentic-rag-docs"

    app_host: str = "0.0.0.0"
    app_port: int = 8000
    log_level: str = "INFO"

    # Retrieval defaults
    top_k: int = 6
    # Max L2 distance for a chunk to count as relevant. Measured on the sample
    # corpus: on-topic questions score 0.54-0.79, off-topic ones 1.35-1.77.
    relevance_threshold: float = 1.2
    chunk_size: int = 800
    chunk_overlap: int = 120


@lru_cache
def get_settings() -> Settings:
    return Settings()
