"""Does the keyword half of hybrid search actually earn its place?

Hybrid retrieval is usually justified by argument rather than measurement:
"vector search misses exact terms." This runs the comparison instead — the same
queries through vector-only and hybrid, with the difference reported.

    python -m src.eval.ab_hybrid

Two things it measures, because they answer different questions:

* **Fact coverage on the golden set** — of the facts an answer must state, how
  many appear anywhere in the retrieved chunks? This is the realistic path,
  with source filtering on.
* **Rank of a known target** — searching the whole corpus for one identifier,
  where does the chunk containing it land? This isolates the case hybrid is
  supposed to win, without routing narrowing the field first.

Requires OpenSearch; FAISS has no keyword half to compare against.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from langchain_openai import OpenAIEmbeddings
from opensearchpy import OpenSearch

from src.config import ROOT_DIR, get_settings
from src.eval.harness import GOLDEN_PATH, mentions
from src.vectorstore.opensearch import OpenSearchVectorStore

# Identifiers of decreasing opacity. `CAP-120` means nothing to an embedding
# model; `payments-v2` contains a real word. The contrast is the finding.
IDENTIFIER_PROBES = [
    ("CAP-120", "CAP-120"),
    ("Tell me about CAP-120", "CAP-120"),
    ("ledger-core", "Ledger Core"),
    ("payments-v2", "payments-v2"),
]


@dataclass
class Coverage:
    hits: int
    total: int

    def __str__(self) -> str:
        return f"{self.hits}/{self.total}"


def _stores() -> tuple[OpenSearchVectorStore, OpenSearchVectorStore]:
    """The same index, queried two ways — so nothing but the method differs."""
    settings = get_settings()
    client = OpenSearch(
        hosts=[{"host": settings.opensearch_host, "port": settings.opensearch_port}],
        use_ssl=settings.opensearch_use_ssl,
        verify_certs=settings.opensearch_verify_certs,
        ssl_show_warn=False,
        timeout=settings.opensearch_timeout,
    )
    index = settings.opensearch_index
    return (
        OpenSearchVectorStore(client, index, hybrid=True),
        OpenSearchVectorStore(client, index, hybrid=False),
    )


def fact_coverage(store, embeddings, golden: list[dict], k: int) -> Coverage:
    """Of the facts each answer must state, how many reached the model at all?

    Deliberately not precision@k: that needs a human to label which chunks are
    relevant per query. This asks the question the system actually cares
    about — did the evidence contain what the answer needs?
    """
    hits = total = 0
    for item in golden:
        vector = embeddings.embed_query(item["question"])
        chunks = store.search(
            vector, k=k, sources=item.get("expected_sources"), query_text=item["question"]
        )
        blob = " ".join(c.chunk.content for c in chunks)
        for fact in item.get("must_mention") or []:
            total += 1
            hits += bool(mentions(blob, fact))
    return Coverage(hits, total)


def rank_of(store, embeddings, query: str, needle: str, k: int) -> int | None:
    """Position of the first chunk containing `needle`, or None if absent.

    No source filter: routing would otherwise narrow the corpus so far that
    both methods succeed and the comparison says nothing.
    """
    vector = embeddings.embed_query(query)
    hits = store.search(vector, k=k, query_text=query)
    return next(
        (i for i, h in enumerate(hits, start=1) if needle.lower() in h.chunk.content.lower()),
        None,
    )


def main() -> None:
    settings = get_settings()
    if settings.vector_store != "opensearch":
        raise SystemExit("Set VECTOR_STORE=opensearch — FAISS has no keyword half to compare.")

    hybrid, vector_only = _stores()
    embeddings = OpenAIEmbeddings(
        model=settings.openai_embedding_model, api_key=settings.openai_api_key
    )
    golden = json.loads((ROOT_DIR / GOLDEN_PATH).read_text(encoding="utf-8")
                        if not GOLDEN_PATH.is_absolute() else GOLDEN_PATH.read_text(encoding="utf-8"))
    k = settings.top_k

    print(f"index: {settings.opensearch_index}  ({hybrid.count():,} chunks)  k={k}\n")

    print("=" * 66)
    print("Fact coverage on the golden set (source filtering ON — the real path)")
    print("=" * 66)
    print(f"  hybrid      : {fact_coverage(hybrid, embeddings, golden, k)}")
    print(f"  vector only : {fact_coverage(vector_only, embeddings, golden, k)}")

    print()
    print("=" * 66)
    print("Rank of a known target (no source filter — whole corpus competing)")
    print("=" * 66)
    for query, needle in IDENTIFIER_PROBES:
        h = rank_of(hybrid, embeddings, query, needle, k)
        v = rank_of(vector_only, embeddings, query, needle, k)
        fmt = lambda r: f"rank {r}" if r else f"not in top {k}"
        flag = "  <-- keyword half decides it" if v is None and h is not None else ""
        print(f'  "{query}"')
        print(f"     hybrid      : {fmt(h)}")
        print(f"     vector only : {fmt(v)}{flag}")


if __name__ == "__main__":
    main()
