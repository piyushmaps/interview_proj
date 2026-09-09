"""Answer cache: exact hits, semantic hits, expiry, eviction.

The cache must never turn a correct answer into a wrong one, so the tests care
most about what it *refuses* to match.
"""

import math

import pytest

from src.api.cache import AnswerCache

# Unit vectors at known angles, so similarity is exact rather than approximate.
def _unit(deg: float) -> list[float]:
    r = math.radians(deg)
    return [math.cos(r), math.sin(r)]


def _payload(answer: str) -> dict:
    return {"question": "q", "answer": answer, "selected_sources": [],
            "routing_rationale": "", "citations": [], "sub_queries": []}


@pytest.fixture
def cache() -> AnswerCache:
    return AnswerCache(max_entries=3, ttl_seconds=60, similarity=0.97)


def test_exact_question_hits(cache):
    cache.put("who owns Payments?", _unit(0), _payload("money-movement"))
    got = cache.get("who owns Payments?", _unit(0))
    assert got["answer"] == "money-movement"
    assert cache.stats.exact_hits == 1


def test_exact_match_ignores_case_and_spacing(cache):
    cache.put("Who Owns Payments?", _unit(0), _payload("money-movement"))
    assert cache.get("  who owns   payments? ", _unit(0)) is not None


def test_reworded_question_hits_semantically(cache):
    cache.put("who owns Payments?", _unit(0), _payload("money-movement"))
    # Different wording, near-identical meaning -> near-identical vector.
    got = cache.get("who owns the Payments capability?", _unit(5))
    assert got["answer"] == "money-movement"
    assert cache.stats.semantic_hits == 1


def test_a_different_question_does_not_hit(cache):
    """The failure that matters: a wrong hit is a wrong answer."""
    cache.put("who owns Payments?", _unit(0), _payload("money-movement"))
    assert cache.get("how do I register a capability?", _unit(45)) is None
    assert cache.stats.misses == 1


def test_entries_expire(cache):
    expiring = AnswerCache(max_entries=3, ttl_seconds=-1, similarity=0.97)
    expiring.put("q", _unit(0), _payload("stale"))
    assert expiring.get("q", _unit(0)) is None


def test_least_recently_used_is_evicted(cache):
    for i in range(4):  # max_entries is 3
        cache.put(f"question {i}", _unit(i * 30), _payload(str(i)))
    assert len(cache) == 3
    assert cache.get("question 0", None) is None      # first one is gone


def test_a_hit_refreshes_recency(cache):
    for i in range(3):
        cache.put(f"question {i}", _unit(i * 30), _payload(str(i)))
    cache.get("question 0", None)                      # touch the oldest
    cache.put("question 3", _unit(120), _payload("3"))
    assert cache.get("question 0", None) is not None   # survived
    assert cache.get("question 1", None) is None       # evicted instead


def test_clear_empties_it(cache):
    cache.put("q", _unit(0), _payload("a"))
    cache.clear()
    assert len(cache) == 0


def test_lookup_without_a_vector_is_exact_only(cache):
    cache.put("who owns Payments?", _unit(0), _payload("money-movement"))
    assert cache.get("who owns the Payments capability?", None) is None


def test_hit_rate_is_reported(cache):
    cache.put("q", _unit(0), _payload("a"))
    cache.get("q", _unit(0))
    cache.get("something else entirely", _unit(90))
    assert cache.stats.hit_rate == pytest.approx(0.5)
