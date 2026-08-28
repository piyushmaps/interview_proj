"""Tracing must be invisible when it is off.

Observability should never become a dependency of the thing it observes: no
keys, a bad host, or a Langfuse outage must all leave the app working normally.
"""

import pytest

from src.config import get_settings
from src.observability import tracing


@pytest.fixture(autouse=True)
def reset_tracing_state():
    """Each test starts from 'not configured'."""
    tracing._client, tracing._enabled = None, False
    yield
    tracing._client, tracing._enabled = None, False
    get_settings.cache_clear()


def _no_keys(monkeypatch):
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "")
    get_settings.cache_clear()


def test_disabled_without_keys(monkeypatch):
    _no_keys(monkeypatch)
    assert tracing.setup_tracing() is False
    assert tracing.is_tracing_enabled() is False


def test_callbacks_are_empty_when_off(monkeypatch):
    _no_keys(monkeypatch)
    tracing.setup_tracing()
    # LangGraph receives an empty callback list, so nothing is instrumented.
    assert tracing.langfuse_callbacks() == []


def test_traced_block_still_runs_when_off(monkeypatch):
    _no_keys(monkeypatch)
    tracing.setup_tracing()
    ran = False
    with tracing.traced("retrieve", as_type="retriever"):
        ran = True
    assert ran


def test_record_retrieval_is_a_noop_when_off(monkeypatch):
    _no_keys(monkeypatch)
    tracing.setup_tracing()
    tracing.record_retrieval("q", ["jira"], [])  # must not raise


def test_flush_is_safe_when_off(monkeypatch):
    _no_keys(monkeypatch)
    tracing.setup_tracing()
    tracing.flush()  # must not raise


def test_unreachable_host_disables_rather_than_crashes(monkeypatch):
    """A Langfuse outage must not take the API down with it."""
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-fake")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-fake")
    monkeypatch.setenv("LANGFUSE_BASE_URL", "http://127.0.0.1:1")
    get_settings.cache_clear()

    assert tracing.setup_tracing() is False
    assert tracing.is_tracing_enabled() is False
    with tracing.traced("retrieve"):
        pass


def test_settings_prefer_base_url_over_host(monkeypatch):
    monkeypatch.setenv("LANGFUSE_BASE_URL", "https://us.cloud.langfuse.com")
    monkeypatch.setenv("LANGFUSE_HOST", "https://cloud.langfuse.com")
    get_settings.cache_clear()
    assert get_settings().langfuse_url == "https://us.cloud.langfuse.com"
