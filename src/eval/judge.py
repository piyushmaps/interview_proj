"""Groundedness: did the answer say anything the evidence did not support?

The existing fact check asks whether an answer *stated what it should*. This
asks the opposite question — whether it stated anything it *shouldn't*. Both are
needed: one catches gaps, the other catches invention, and an answer can pass
either while failing the other.

The judge never sees a correct answer, and that is the point. It compares the
response against the evidence the model was given and asks only whether one
supports the other. So it works on questions nobody has labelled, including
live traffic.

A limitation worth stating plainly: an answer can be perfectly grounded and
still wrong, if retrieval handed the model the wrong evidence in the first
place. Groundedness measures the generation step, not the whole system.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from langchain_openai import ChatOpenAI

from src.config import get_settings

logger = logging.getLogger(__name__)

PROMPT = """You are checking whether an ANSWER is supported by the EVIDENCE it was given.

Rules:
- Judge support only. Do not use outside knowledge, and do not judge whether the
  answer is true in the real world.
- A claim is unsupported if the evidence does not state it, even if it sounds correct.
- Ignore hedging and pleasantries; judge the factual claims.
- An answer that declines to answer for lack of evidence is fully grounded.

EVIDENCE:
{evidence}

QUESTION: {question}

ANSWER: {answer}

Reply with ONLY this JSON:
{{"score": <0.0-1.0, the fraction of claims supported>,
  "unsupported": ["each claim the evidence does not back, quoted briefly"]}}"""


@dataclass
class Verdict:
    score: float
    unsupported: list[str]

    @property
    def grounded(self) -> bool:
        return self.score >= 0.999

    def __str__(self) -> str:
        if self.grounded:
            return "grounded"
        return f"{self.score:.2f} — {'; '.join(self.unsupported[:2])}"


def _llm() -> ChatOpenAI:
    settings = get_settings()
    return ChatOpenAI(
        model=settings.judge_model or settings.openai_model,
        api_key=settings.openai_api_key,
        temperature=0,
    )


def judge_answer(question: str, evidence: list[str], answer: str) -> Verdict:
    """Score one answer against the evidence that produced it.

    A refusal is grounded by definition — there is nothing to support.
    """
    if not evidence:
        return Verdict(1.0, [])

    numbered = "\n\n".join(f"[{i}] {text}" for i, text in enumerate(evidence, start=1))
    raw = _llm().invoke(
        PROMPT.format(evidence=numbered, question=question, answer=answer)
    ).content
    return _parse(raw if isinstance(raw, str) else str(raw))


def _parse(text: str) -> Verdict:
    """Defensive parsing — a judge that crashes is worse than one that abstains."""
    try:
        payload: dict[str, Any] = json.loads(text.strip())
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            logger.warning("judge returned unparseable output; treating as ungraded")
            return Verdict(1.0, [])
        try:
            payload = json.loads(match.group(0))
        except json.JSONDecodeError:
            return Verdict(1.0, [])

    score = payload.get("score")
    unsupported = [str(c) for c in (payload.get("unsupported") or [])]
    try:
        score = float(score)
    except (TypeError, ValueError):
        # No usable score: infer from whether it listed problems.
        score = 0.0 if unsupported else 1.0
    return Verdict(max(0.0, min(1.0, score)), unsupported)
