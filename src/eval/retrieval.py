"""Retrieval quality: at what rank does the needed evidence actually arrive?

The textbook metrics — recall@k and precision@k — both require knowing which
chunks are relevant to each question, which means a human labelling chunks.
That annotation does not exist here, so rather than claim those numbers, this
measures something the golden set already supports:

    For each fact an answer must state, at what rank does the first chunk
    containing that fact appear?

From those ranks everything else follows. Recall@k is the share of facts whose
first hit lands at or before k, and sweeping k gives the curve that says where
`top_k` should sit — the point past which more chunks stop buying anything.

    python -m src.eval.retrieval

What it deliberately does not report is precision@k. Precision needs to know
which of the retrieved chunks were *unnecessary*, and only a human can say that.
A made-up precision number is worse than an absent one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from langchain_openai import OpenAIEmbeddings

from src.config import get_settings
from src.eval.harness import GOLDEN_PATH, mentions
from src.retrieval.retriever import SourceRetriever

# Where to sample the curve. The last value doubles as "how deep would we have
# had to look", which is what tells you whether a miss is fixable by raising k.
K_SWEEP = (1, 3, 6, 10, 20)


@dataclass
class FactResult:
    """Where one required fact first showed up, if it did."""

    question_id: str
    fact: str
    rank: int | None  # None = not found within the deepest k searched

    @property
    def found(self) -> bool:
        return self.rank is not None


@dataclass
class RetrievalReport:
    results: list[FactResult] = field(default_factory=list)

    def recall_at(self, k: int) -> float:
        """Share of required facts whose evidence arrives at or before rank k."""
        if not self.results:
            return 0.0
        hits = sum(1 for r in self.results if r.found and r.rank <= k)
        return hits / len(self.results)

    @property
    def misses(self) -> list[FactResult]:
        return [r for r in self.results if not r.found]

    def render(self) -> str:
        lines = ["  k    recall@k", "  ---  --------"]
        previous = None
        for k in K_SWEEP:
            recall = self.recall_at(k)
            # Flag the point where more depth stops helping — that is the
            # argument for whatever top_k is set to.
            flat = "   <- no gain over previous k" if previous == recall else ""
            lines.append(f"  {k:<4} {recall:>6.2f}{flat}")
            previous = recall
        return "\n".join(lines)


def measure(retriever: SourceRetriever, golden: list[dict], max_k: int) -> RetrievalReport:
    report = RetrievalReport()
    for item in golden:
        facts = item.get("must_mention") or []
        if not facts:
            continue
        # Retrieved once per question at the deepest k, then every smaller k is
        # a prefix of that list — so the sweep costs one search, not five.
        chunks = retriever.retrieve(
            item["question"],
            sources=item.get("expected_sources"),
            k=max_k,
            min_similarity=None,  # measuring retrieval, not the relevance policy
        )
        for fact in facts:
            rank = next(
                (i for i, c in enumerate(chunks, start=1) if mentions(c.content, fact)),
                None,
            )
            report.results.append(FactResult(item["id"], fact, rank))
    return report


def main() -> None:
    settings = get_settings()
    golden = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    retriever = SourceRetriever(
        embeddings=OpenAIEmbeddings(
            model=settings.openai_embedding_model, api_key=settings.openai_api_key
        )
    )
    max_k = max(K_SWEEP)
    report = measure(retriever, golden, max_k)

    print(f"\nretrieval quality over {len(report.results)} required facts")
    print(f"(configured top_k = {settings.top_k})\n")
    print(report.render())

    if report.misses:
        print(f"\n  not found within k={max_k} — these are indexing problems,")
        print("  not depth problems, and raising top_k will not fix them:")
        for miss in report.misses:
            print(f"    {miss.question_id}: {miss.fact!r}")
    else:
        print(f"\n  every required fact was retrievable within k={max_k}.")


if __name__ == "__main__":
    main()
