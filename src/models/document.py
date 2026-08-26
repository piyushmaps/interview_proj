"""Normalized document schema shared across all connectors."""

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class SourceType(str, Enum):
    JIRA = "jira"
    SLACK = "slack"
    DEVPORTAL = "devportal"
    CITY_MAP = "city_map"


class Document(BaseModel):
    """Canonical unit of knowledge after connector normalization."""

    id: str
    source: SourceType
    title: str
    body: str
    url: str
    author: str | None = None
    updated_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    def to_text(self) -> str:
        """Flat text used for embedding / BM25."""
        header = f"[{self.source.value.upper()}] {self.title}"
        return f"{header}\n\n{self.body}".strip()
