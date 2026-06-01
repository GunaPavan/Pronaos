"""EvalRunner: end-to-end aggregation with mocked candidate + scorer.

The runner is mostly orchestration glue, so we test the
behavioural contract: error rows excluded from aggregates, per-
category breakdown computed correctly, pass-rate uses the threshold,
JSON round-trip works.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from pronaos.eval.data import EvalCase, GoldenSet
from pronaos.eval.runner import EvalRunner
from pronaos.eval.scorer import Scorer, ScoreResult

CANDIDATE_URL = "http://gateway.local/v1/chat/completions"


class StubScorer(Scorer):
    """Returns canned scores keyed on the candidate response. Lets the
    test pin exact aggregates without an actual LLM in the loop."""

    def __init__(self, response_to_score: dict[str, float]) -> None:
        self._scores = response_to_score

    async def score(self, *, prompt: str, expected: str, candidate: str) -> ScoreResult:
        if candidate in self._scores:
            return ScoreResult(score=self._scores[candidate], justification="canned", raw="")
        return ScoreResult(score=0.0, justification="", judge_error="unknown candidate")


def _candidate_response(content: str) -> dict:
    return {
        "id": "x",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}}],
    }


def _build_golden_set() -> GoldenSet:
    return GoldenSet(
        name="stub",
        cases=[
            EvalCase(id="a", category="factual", prompt="a?", expected="..."),
            EvalCase(id="b", category="factual", prompt="b?", expected="..."),
            EvalCase(id="c", category="reasoning", prompt="c?", expected="..."),
        ],
    )


# --------------------------------------------------------------------------- #
# Aggregation                                                                 #
# --------------------------------------------------------------------------- #


@respx.mock
@pytest.mark.asyncio
async def test_summary_aggregates_scores() -> None:
    """Three cases scoring 0.9, 0.8, 0.4. Mean = 0.7, median = 0.8."""
    respx.post(CANDIDATE_URL).mock(
        side_effect=[
            httpx.Response(200, json=_candidate_response("answer-a")),
            httpx.Response(200, json=_candidate_response("answer-b")),
            httpx.Response(200, json=_candidate_response("answer-c")),
        ]
    )
    scorer = StubScorer({"answer-a": 0.9, "answer-b": 0.8, "answer-c": 0.4})

    runner = EvalRunner(
        candidate_base_url="http://gateway.local",
        candidate_api_key="pn_live_test",
        candidate_model="groq/llama-3.1-8b-instant",
        scorer=scorer,
        judge_model_id="anthropic/claude-haiku-4-5",
        pass_threshold=0.7,
    )
    summary = await runner.run(_build_golden_set())

    assert summary.total_cases == 3
    assert summary.scored_cases == 3
    assert summary.overall_mean == pytest.approx(0.7)
    assert summary.overall_median == pytest.approx(0.8)
    # 2 of 3 cases scored ≥ 0.7
    assert summary.overall_pass_rate == pytest.approx(2 / 3)


@respx.mock
@pytest.mark.asyncio
async def test_candidate_error_excluded_from_aggregate() -> None:
    """If the candidate call 5xx's, that row is marked candidate_error
    and excluded from the mean. Otherwise a flaky provider would
    artificially depress eval scores."""
    respx.post(CANDIDATE_URL).mock(
        side_effect=[
            httpx.Response(200, json=_candidate_response("ok-1")),
            httpx.Response(500, text="provider down"),
            httpx.Response(200, json=_candidate_response("ok-2")),
        ]
    )
    scorer = StubScorer({"ok-1": 1.0, "ok-2": 0.8})

    runner = EvalRunner(
        candidate_base_url="http://gateway.local",
        candidate_api_key="pn_live_test",
        candidate_model="groq/llama-3.1-8b-instant",
        scorer=scorer,
        judge_model_id="anthropic/claude-haiku-4-5",
    )
    summary = await runner.run(_build_golden_set())

    assert summary.total_cases == 3
    assert summary.scored_cases == 2
    assert summary.candidate_errors == 1
    assert summary.overall_mean == pytest.approx(0.9)


@respx.mock
@pytest.mark.asyncio
async def test_category_breakdown() -> None:
    """Two factual cases (mean 0.85) + one reasoning case (mean 0.4)
    must produce two category summaries with the right per-axis means.
    This is the "which axis is degrading?" view."""
    respx.post(CANDIDATE_URL).mock(
        side_effect=[
            httpx.Response(200, json=_candidate_response("answer-a")),
            httpx.Response(200, json=_candidate_response("answer-b")),
            httpx.Response(200, json=_candidate_response("answer-c")),
        ]
    )
    scorer = StubScorer({"answer-a": 0.9, "answer-b": 0.8, "answer-c": 0.4})

    runner = EvalRunner(
        candidate_base_url="http://gateway.local",
        candidate_api_key="pn_live_test",
        candidate_model="m",
        scorer=scorer,
        judge_model_id="j",
    )
    summary = await runner.run(_build_golden_set())

    by_name = {c.name: c for c in summary.categories}
    assert by_name["factual"].count == 2
    assert by_name["factual"].mean == pytest.approx(0.85)
    assert by_name["reasoning"].count == 1
    assert by_name["reasoning"].mean == pytest.approx(0.4)


# --------------------------------------------------------------------------- #
# Serialization                                                                #
# --------------------------------------------------------------------------- #


@respx.mock
@pytest.mark.asyncio
async def test_summary_round_trips_json(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """``save_json`` produces a file that re-parses as the same shape.
    This is what the CI gate (Phase 9.2) will diff."""
    respx.post(CANDIDATE_URL).mock(
        return_value=httpx.Response(200, json=_candidate_response("ans"))
    )
    scorer = StubScorer({"ans": 0.75})

    runner = EvalRunner(
        candidate_base_url="http://gateway.local",
        candidate_api_key="pn_live_test",
        candidate_model="m",
        scorer=scorer,
        judge_model_id="j",
    )
    summary = await runner.run(
        GoldenSet(
            name="rt",
            cases=[EvalCase(id="only", category="factual", prompt="?", expected="...")],
        )
    )
    out = tmp_path / "result.json"
    summary.save_json(out)

    reparsed = json.loads(out.read_text(encoding="utf-8"))
    assert reparsed["golden_set"] == "rt"
    assert reparsed["overall_mean"] == pytest.approx(0.75)
    assert len(reparsed["rows"]) == 1
