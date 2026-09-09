"""Answer cache — the cheapest thing you can do about cost and latency.

An internal knowledge assistant is asked the same handful of questions over and
over: who owns this service, how do I register a capability, why is my build
failing. Every repeat currently costs two LLM calls and about three seconds, to
produce an answer that already exists.

Two layers, because questions repeat in two different ways:

* **Exact** — the identical string, usually a retry or a shared link. A dict
  lookup, effectively free.
* **Semantic** — "who owns Payments" and "who owns the Payments capability" are
  the same question. Matched on the question's embedding, which retrieval has
  to compute anyway.

Deliberately in-memory. A single process is enough to prove the hit rate, and
the interface is small enough that swapping in Redis — which is what you need
once there is more than one API instance — touches this file only.

What is cached is the whole response, citations included, so a hit is
indistinguishable from a miss apart from being instant.
"""

from __future__ import annotations

import hashlib
import logging
import math
import time
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class CacheStats:
    exact_hits: int = 0
    semantic_hits: int = 0
    misses: int = 0

    @property
    def hit_rate(self) -> float:
        total = self.exact_hits + self.semantic_hits + self.misses
        return (self.exact_hits + self.semantic_hits) / total if total else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "exact_hits": self.exact_hits,
            "semantic_hits": self.semantic_hits,
            "misses": self.misses,
            "hit_rate": round(self.hit_rate, 3),
        }


@dataclass
class _Entry:
    question: str
    vector: list[float]
    payload: dict[str, Any]
    stored_at: float = field(default_factory=time.monotonic)

    def expired(self, ttl: float) -> bool:
        return time.monotonic() - self.stored_at > ttl


def _key(question: str) -> str:
    """Normalised so trailing whitespace and casing do not split the cache."""
    return hashlib.sha256(" ".join(question.lower().split()).encode("utf-8")).hexdigest()


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


class AnswerCache:
    """Bounded, TTL'd cache of whole `/ask` responses.

    The TTL is the invalidation story: an answer can go stale when the index is
    re-ingested, and rather than track which documents fed which answer, entries
    simply expire. Set it shorter than the ingestion interval and staleness is
    bounded by construction.
    """

    def __init__(self, max_entries: int, ttl_seconds: float, similarity: float) -> None:
        self.max_entries = max_entries
        self.ttl = ttl_seconds
        self.similarity = similarity
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        self.stats = CacheStats()

    def get(self, question: str, vector: Sequence[float] | None = None) -> dict[str, Any] | None:
        key = _key(question)
        entry = self._entries.get(key)
        if entry is not None and not entry.expired(self.ttl):
            self._entries.move_to_end(key)
            self.stats.exact_hits += 1
            return entry.payload
        if entry is not None:
            del self._entries[key]

        if vector is None:
            self.stats.misses += 1
            return None

        # Nearest cached question. Linear, which is fine at these sizes — a few
        # thousand comparisons is microseconds, and a vector index for the cache
        # would cost more to maintain than it saves.
        best_key, best_score = None, 0.0
        for cached_key, cached in list(self._entries.items()):
            if cached.expired(self.ttl):
                del self._entries[cached_key]
                continue
            score = _cosine(vector, cached.vector)
            if score > best_score:
                best_key, best_score = cached_key, score

        if best_key is not None and best_score >= self.similarity:
            self._entries.move_to_end(best_key)
            self.stats.semantic_hits += 1
            logger.debug("semantic cache hit (%.3f): %r", best_score, question)
            return self._entries[best_key].payload

        self.stats.misses += 1
        return None

    def put(self, question: str, vector: Sequence[float], payload: dict[str, Any]) -> None:
        self._entries[_key(question)] = _Entry(question, list(vector), payload)
        self._entries.move_to_end(_key(question))
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)  # evict least recently used

    def clear(self) -> None:
        """Call after re-ingestion to drop answers built on the old index."""
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)
