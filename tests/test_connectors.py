"""Smoke tests for sample connectors (no API key required)."""

from src.connectors import (
    CityMapConnector,
    DevPortalConnector,
    JiraConnector,
    SlackConnector,
)
from src.ingestion.chunking import to_langchain_chunks
from src.models.document import SourceType


def test_all_connectors_load():
    docs = []
    docs.extend(JiraConnector().load())
    docs.extend(SlackConnector().load())
    docs.extend(DevPortalConnector().load())
    docs.extend(CityMapConnector().load())

    assert len(docs) >= 10
    sources = {d.source for d in docs}
    assert sources == {
        SourceType.JIRA,
        SourceType.SLACK,
        SourceType.DEVPORTAL,
        SourceType.CITY_MAP,
    }


def test_chunking_preserves_source_metadata():
    docs = JiraConnector().load() + DevPortalConnector().load()
    chunks = to_langchain_chunks(docs)
    assert chunks
    assert all("source" in c.metadata for c in chunks)
    assert all("url" in c.metadata for c in chunks)
