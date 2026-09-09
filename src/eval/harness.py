"""Golden-set evaluation: scores routing and grounding as separate metrics.

Run:
    python -m src.eval.harness                 # score the agent
    python -m src.eval.harness --baseline      # also score no-routing RAG
    python -m src.eval.harness --fail-under 0.8

Two axes, deliberately kept apart:

* **Routing**  — did the router select the sources the question needs?
  Scored as recall/precision over the expected set, *not* exact match. A router
  that picks `devportal + city_map` when only `devportal` was needed still
  surfaces every fact; exact-match would score that a failure and report ~30%
  accuracy for a system answering everything correctly.
* **Grounding** — did the answer state the facts it must mention?
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.agent.graph import run_agent
from src.agent.nodes import VALID_SOURCES, synthesize_answer
from src.config import ROOT_DIR
from src.eval.judge import Verdict, judge_answer
from src.retrieval.retriever import SourceRetriever

GOLDEN_PATH = ROOT_DIR / "data" / "eval" / "golden.json"

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalize(text: str) -> str:
    """Casefold and collapse every non-alphanumeric run to a single space.

    Golden facts are written as raw identifiers (`alex.chen`, `CAP-120`,
    `money-movement`) while the model naturally humanises them (`Alex Chen`,
    `CAP 120`, `money movement`). Normalising *both* sides means the harness
    measures whether the fact is present, not how it was punctuated — without
    it, correct answers are scored as misses.
    """
    return _NON_ALNUM.sub(" ", text.casefold()).strip()


def mentions(answer: str, fact: str) -> bool:
    return normalize(fact) in normalize(answer)


@dataclass
class Result:
    qid: str
    question: str
    expected_sources: set[str]
    routed_sources: set[str]
    answer: str
    facts_hit: list[str] = field(default_factory=list)
    facts_missed: list[str] = field(default_factory=list)
    n_citations: int = 0
    verdict: Verdict | None = None

    @property
    def routing_recall(self) -> float:
        if not self.expected_sources:
            return 1.0
        return len(self.expected_sources & self.routed_sources) / len(self.expected_sources)

    @property
    def routing_precision(self) -> float:
        if not self.routed_sources:
            return 0.0
        return len(self.expected_sources & self.routed_sources) / len(self.routed_sources)

    @property
    def routing_exact(self) -> bool:
        return self.expected_sources == self.routed_sources


def _score_facts(result: Result, must_mention: list[str]) -> None:
    for fact in must_mention:
        (result.facts_hit if mentions(result.answer, fact) else result.facts_missed).append(fact)


def evaluate(
    golden: list[dict[str, Any]], retriever: SourceRetriever, judge: bool = False
) -> list[Result]:
    results: list[Result] = []
    for item in golden:
        out = run_agent(item["question"], retriever=retriever)
        r = Result(
            qid=item["id"],
            question=item["question"],
            expected_sources=set(item.get("expected_sources") or []),
            routed_sources=set(out.get("selected_sources") or []),
            answer=out.get("answer", ""),
            n_citations=len(out.get("citations") or []),
        )
        _score_facts(r, item.get("must_mention") or [])
        if judge:
            # Judged against the evidence the model actually saw, so a low
            # score means invention rather than bad retrieval.
            evidence = [c.content for c in retriever.retrieve(
                item["question"], sources=out.get("selected_sources")
            )]
            r.verdict = judge_answer(item["question"], evidence, r.answer)
        results.append(r)
    return results


def evaluate_baseline(golden: list[dict[str, Any]], retriever: SourceRetriever) -> list[Result]:
    """Plain RAG: one unfiltered search over every source, no routing, no
    decomposition. This is the control that tells you whether the agent layer
    is actually earning its latency and token cost."""
    results: list[Result] = []
    for item in golden:
        chunks = retriever.retrieve(item["question"])
        state = synthesize_answer(
            {
                "question": item["question"],
                "retrieved": chunks,
                "selected_sources": [],
                "routing_rationale": "baseline: no routing",
            }
        )
        r = Result(
            qid=item["id"],
            question=item["question"],
            expected_sources=set(item.get("expected_sources") or []),
            # Plain RAG effectively 'selects' every source: perfect routing
            # recall, worst-case precision. That contrast is the point.
            routed_sources=set(VALID_SOURCES),
            answer=state.get("answer", ""),
            n_citations=len(state.get("citations") or []),
        )
        _score_facts(r, item.get("must_mention") or [])
        results.append(r)
    return results


def fact_recall(results: list[Result]) -> float:
    hit = sum(len(r.facts_hit) for r in results)
    total = hit + sum(len(r.facts_missed) for r in results)
    return hit / total if total else 1.0


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def report(results: list[Result], label: str = "AGENT") -> None:
    print(f"\n{'=' * 74}\n{label}\n{'=' * 74}")
    print(f"{'id':<6}{'route R':>9}{'route P':>9}{'exact':>7}{'facts':>8}{'cites':>7}  missing")
    for r in results:
        facts = f"{len(r.facts_hit)}/{len(r.facts_hit) + len(r.facts_missed)}"
        print(
            f"{r.qid:<6}{r.routing_recall:>9.2f}{r.routing_precision:>9.2f}"
            f"{('yes' if r.routing_exact else 'no'):>7}{facts:>8}{r.n_citations:>7}  "
            f"{', '.join(r.facts_missed) or '-'}"
        )
    print("-" * 74)
    print(f"  routing recall    : {_mean([r.routing_recall for r in results]):.2f}")
    print(f"  routing precision : {_mean([r.routing_precision for r in results]):.2f}")
    print(f"  routing exact     : {sum(r.routing_exact for r in results)}/{len(results)}")
    print(f"  fact recall       : {fact_recall(results):.2f}")
    judged = [r for r in results if r.verdict is not None]
    if judged:
        mean = sum(r.verdict.score for r in judged) / len(judged)
        ungrounded = [r for r in judged if not r.verdict.grounded]
        print(f"  groundedness      : {mean:.2f} "
              f"({len(judged) - len(ungrounded)}/{len(judged)} fully grounded)")
        for r in ungrounded:
            print(f"      {r.qid}: {r.verdict}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Score the agent against the golden set.")
    parser.add_argument("--golden", type=Path, default=GOLDEN_PATH)
    parser.add_argument("--judge", action="store_true",
                        help="score each answer for groundedness against its evidence")
    parser.add_argument("--baseline", action="store_true",
                        help="also score plain RAG (no routing) for comparison")
    parser.add_argument("--fail-under", type=float, default=None,
                        help="exit non-zero if fact recall falls below this")
    args = parser.parse_args()

    golden = json.loads(args.golden.read_text(encoding="utf-8"))
    retriever = SourceRetriever()

    results = evaluate(golden, retriever, judge=args.judge)
    report(results, "AGENT (route -> retrieve -> synthesize)")

    if args.baseline:
        base = evaluate_baseline(golden, retriever)
        report(base, "BASELINE (plain RAG, no routing)")
        delta = fact_recall(results) - fact_recall(base)
        print(f"\n  fact-recall lift from the agent layer: {delta:+.2f}")

    if args.fail_under is not None and fact_recall(results) < args.fail_under:
        raise SystemExit(f"fact recall {fact_recall(results):.2f} < {args.fail_under}")


if __name__ == "__main__":
    main()
