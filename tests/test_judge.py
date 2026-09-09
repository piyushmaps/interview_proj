"""Groundedness judging: parsing, and the cases that must not need an LLM."""

import pytest

from src.eval import judge as judge_mod
from src.eval.judge import Verdict, judge_answer


def test_refusal_needs_no_llm_call(monkeypatch):
    """No evidence means nothing to contradict — grounded by definition, and
    calling the model here would be spending money to learn nothing."""
    monkeypatch.setattr(judge_mod, "_llm", lambda: pytest.fail("should not call the LLM"))
    verdict = judge_answer("what is langchain?", [], "I couldn't find relevant evidence.")
    assert verdict.grounded


def test_parses_a_clean_verdict():
    v = judge_mod._parse('{"score": 0.5, "unsupported": ["owned by identity team"]}')
    assert v.score == 0.5
    assert v.unsupported == ["owned by identity team"]
    assert not v.grounded


def test_parses_json_wrapped_in_prose():
    v = judge_mod._parse('Here is my assessment:\n{"score": 1.0, "unsupported": []}\nDone.')
    assert v.grounded


def test_unparseable_output_abstains_rather_than_crashing():
    """A broken judge must not fail the eval run — it just has no opinion."""
    v = judge_mod._parse("the model rambled and returned no json at all")
    assert v.grounded
    assert v.unsupported == []


def test_missing_score_is_inferred_from_the_complaints():
    assert judge_mod._parse('{"unsupported": ["a claim"]}').score == 0.0
    assert judge_mod._parse('{"unsupported": []}').grounded


def test_score_is_clamped():
    assert judge_mod._parse('{"score": 5}').score == 1.0
    assert judge_mod._parse('{"score": -2}').score == 0.0


def test_verdict_renders_the_problem():
    assert str(Verdict(1.0, [])) == "grounded"
    assert "identity team" in str(Verdict(0.5, ["owned by identity team"]))
