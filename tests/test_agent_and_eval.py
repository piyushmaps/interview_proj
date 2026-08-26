"""Citation filtering (agent) and fact-matching (eval harness). No API key needed."""

from types import SimpleNamespace

import pytest

from src.agent import nodes
from src.eval.harness import Result, fact_recall, mentions, normalize
from src.retrieval.retriever import RetrievedChunk


@pytest.fixture
def evidence() -> list[RetrievedChunk]:
    return [
        RetrievedChunk(content=f"body {i}", source="jira", title=f"t{i}", url=f"u{i}")
        for i in range(1, 5)
    ]


def _fake_llm(answer: str):
    return lambda: SimpleNamespace(invoke=lambda prompt: SimpleNamespace(content=answer))


def test_citations_limited_to_those_the_answer_cites(monkeypatch, evidence):
    monkeypatch.setattr(nodes, "_llm", _fake_llm("Owned by money-movement [2], see also [4]."))
    out = nodes.synthesize_answer({"question": "q", "retrieved": evidence})

    # Four chunks retrieved, two cited -> two citation links, original indices kept.
    assert [c["index"] for c in out["citations"]] == [2, 4]


def test_uncited_answer_falls_back_to_all_evidence(monkeypatch, evidence):
    monkeypatch.setattr(nodes, "_llm", _fake_llm("An answer with no markers."))
    out = nodes.synthesize_answer({"question": "q", "retrieved": evidence})
    assert len(out["citations"]) == 4


def test_no_evidence_short_circuits_without_calling_the_model(evidence):
    out = nodes.synthesize_answer({"question": "q", "retrieved": []})
    assert out["citations"] == []
    assert "couldn't find relevant evidence" in out["answer"]


@pytest.mark.parametrize(
    "fact,answer",
    [
        ("alex.chen", "with Alex Chen as the owner lead"),   # handle -> human name
        ("money-movement", "owned by the money movement team"),
        ("CAP-120", "ticket CAP 120 is open"),
        ("city map", "the city-map node is marked deprecated"),
    ],
)
def test_fact_matching_ignores_punctuation_and_case(fact, answer):
    assert mentions(answer, fact)


def test_fact_matching_still_rejects_absent_facts():
    assert not mentions("The Payments capability is owned by money-movement.", "alex.chen")


def test_normalize_collapses_separator_runs():
    assert normalize("CAP-120 / Auth_Gateway") == "cap 120 auth gateway"


def test_routing_scored_by_overlap_not_exact_match():
    # Router picked a superset of what was needed: full recall, partial precision.
    r = Result(qid="q3", question="?", expected_sources={"devportal"},
               routed_sources={"devportal", "city_map"}, answer="")
    assert r.routing_recall == 1.0
    assert r.routing_precision == 0.5
    assert r.routing_exact is False


def test_fact_recall_aggregates_across_questions():
    a = Result("q1", "?", set(), set(), "", facts_hit=["x", "y"], facts_missed=["z"])
    b = Result("q2", "?", set(), set(), "", facts_hit=["p"], facts_missed=[])
    assert fact_recall([a, b]) == pytest.approx(3 / 4)
