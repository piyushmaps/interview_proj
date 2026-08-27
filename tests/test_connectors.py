"""Smoke tests for sample connectors (no API key required)."""

from pathlib import Path

from src.connectors import (
    CityMapConnector,
    DevPortalConnector,
    JiraConnector,
    SlackConnector,
)
from src.ingestion.chunking import iter_chunks
from src.models.document import SourceType

CITY_MAP_FIXTURES = Path(__file__).parent / "fixtures" / "city_map"


def test_all_connectors_load():
    docs = []
    docs.extend(JiraConnector().load())
    docs.extend(SlackConnector().load())
    docs.extend(DevPortalConnector().load())
    docs.extend(CityMapConnector(path=CITY_MAP_FIXTURES).load())

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
    chunks = list(iter_chunks(docs))
    assert chunks
    # Citation fields are first-class on Chunk, not buried in metadata.
    assert all(c.source for c in chunks)
    assert all(c.url for c in chunks)
    assert all(c.doc_id and c.id.startswith(c.doc_id) for c in chunks)
