"""MultiJudgeRunner tests (Phase 23).

Two layers:

1. **Pure-math agreement tests.** The agreement metrics (mean abs delta,
   within-epsilon rate, Cohen's kappa) are pure functions over rows.
   We construct rows directly and assert exact numbers — no httpx, no
   asyncio, no provider mocks. This pins the formula so a regression
   in the math is caught immediately.

2. **End-to-end runner tests.** Build a small golden set, stub the
   candidate call with respx and the judges with StubScorer, run the
   full pipeline, and assert on the aggregate. This verifies the glue
   that fans out one candidate response to N concurrent judges.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from pronaos.eval.data import EvalCase, GoldenSet
from pronaos.eval.multi_judge import (
    JudgeVerdict,
    MultiJudgeEvalRow,
    MultiJudgeRunner,
    _cohens_kappa,
    _pair_agreement,
)
from pronaos.eval.scorer import Scorer, ScoreResult

CANDIDATE_URL = "http://gateway.local/v1/chat/completions"


# --------------------------------------------------------------------------- #
# Test doubles                                                                #
# --------------------------------------------------------------------------- #


class StubScorer(Scorer):
    """Returns canned scores keyed on the candidate response.

    Lets a test pin exact agreement metrics without an actual LLM in
    the loop. Different StubScorer instances act as different judges.
    """

    def __init__(self, scores: dict[str, float]) -> None:
        self._scores = scores

    async def score(self, *, prompt: str, expected: str, candidate: str) -> ScoreResult:
        if candidate not in self._scores:
            return ScoreResult(score=0.0, justification="", judge_error="unknown")
        return ScoreResult(score=self._scores[candidate], justification="canned")


def _candidate_response(text: str) -> dict:
    return {
        "id": "x",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}}],
    }


def _mk_row(case_id: str, *verdicts: tuple[str, float]) -> MultiJudgeEvalRow:
    """Build a row directly. Each verdict is (judge_id, score)."""
    return MultiJudgeEvalRow(
        case_id=case_id,
        category="test",
        prompt="?",
        candidate_response="x",
        verdicts=[JudgeVerdict(judge_id=j, score=s) for j, s in verdicts],
    )


# --------------------------------------------------------------------------- #
# _cohens_kappa                                                                #
# --------------------------------------------------------------------------- #


class TestCohensKappa:
    def test_perfect_agreement(self) -> None:
        """Both judges classify every case the same way → κ = 1."""
        a = [True, True, False, True, False]
        b = [True, True, False, True, False]
        assert _cohens_kappa(a, b) == pytest.approx(1.0)

    def test_no_better_than_chance(self) -> None:
        """When marginals match observed agreement, κ = 0."""
        # 4 cases: both pass, both fail, A pass B fail, A fail B pass.
        # P_o = 2/4 = 0.5
        # a_pass = 2/4 = 0.5, b_pass = 2/4 = 0.5
        # P_e = 0.5*0.5 + 0.5*0.5 = 0.5
        # κ = (0.5 - 0.5) / (1 - 0.5) = 0.0
        a = [True, False, True, False]
        b = [True, False, False, True]
        assert _cohens_kappa(a, b) == pytest.approx(0.0)

    def test_empty_returns_zero(self) -> None:
        """No data → no signal → safe sentinel."""
        assert _cohens_kappa([], []) == 0.0

    def test_degenerate_marginals_return_zero(self) -> None:
        """Both judges pass everything → kappa is undefined; we return 0."""
        a = [True, True, True, True]
        b = [True, True, True, True]
        assert _cohens_kappa(a, b) == 0.0

    def test_mismatched_lengths_return_zero(self) -> None:
        """Defensive: never propagate length mismatch."""
        assert _cohens_kappa([True, False], [True]) == 0.0


# --------------------------------------------------------------------------- #
# _pair_agreement                                                              #
# --------------------------------------------------------------------------- #


class TestPairAgreement:
    def test_perfect_agreement_yields_zero_delta(self) -> None:
        rows = [
            _mk_row("a", ("judge1", 0.9), ("judge2", 0.9)),
            _mk_row("b", ("judge1", 0.5), ("judge2", 0.5)),
            _mk_row("c", ("judge1", 1.0), ("judge2", 1.0)),
        ]
        result = _pair_agreement(rows, "judge1", "judge2", epsilon=0.1, pass_threshold=0.7)
        assert result.n == 3
        assert result.mean_abs_delta == pytest.approx(0.0)
        assert result.within_epsilon_rate == 1.0
        # Mixed pass/fail with perfect alignment → κ = 1.0
        assert result.cohens_kappa == pytest.approx(1.0)

    def test_constant_offset_inflates_delta(self) -> None:
        """Judge B always scores 0.2 above judge A → mean Δ = 0.2."""
        rows = [
            _mk_row("a", ("judge1", 0.5), ("judge2", 0.7)),
            _mk_row("b", ("judge1", 0.7), ("judge2", 0.9)),
        ]
        result = _pair_agreement(rows, "judge1", "judge2", epsilon=0.1, pass_threshold=0.7)
        assert result.mean_abs_delta == pytest.approx(0.2)
        # Both pairs have delta=0.2 > epsilon=0.1, so within-eps rate = 0.
        assert result.within_epsilon_rate == 0.0

    def test_within_epsilon_partial(self) -> None:
        """Some pairs within ε, some not."""
        rows = [
            _mk_row("a", ("judge1", 0.5), ("judge2", 0.55)),  # Δ=0.05 ≤ 0.1
            _mk_row("b", ("judge1", 0.5), ("judge2", 0.8)),  # Δ=0.30 > 0.1
            _mk_row("c", ("judge1", 0.9), ("judge2", 0.85)),  # Δ=0.05 ≤ 0.1
        ]
        result = _pair_agreement(rows, "judge1", "judge2", epsilon=0.1, pass_threshold=0.7)
        # 2 of 3 within ε
        assert result.within_epsilon_rate == pytest.approx(2 / 3)

    def test_errored_verdict_excluded_from_pair(self) -> None:
        """A row where one judge errored is dropped from the pair's n."""
        good = _mk_row("a", ("judge1", 0.9), ("judge2", 0.9))
        partial = MultiJudgeEvalRow(
            case_id="b",
            category="test",
            prompt="?",
            candidate_response="x",
            verdicts=[
                JudgeVerdict(judge_id="judge1", score=0.5),
                JudgeVerdict(judge_id="judge2", score=0.0, judge_error="network: refused"),
            ],
        )
        result = _pair_agreement(
            [good, partial], "judge1", "judge2", epsilon=0.1, pass_threshold=0.7
        )
        assert result.n == 1

    def test_candidate_error_row_excluded(self) -> None:
        """If the candidate call itself failed, the row is dropped entirely
        — no judge could have scored a non-response."""
        good = _mk_row("a", ("judge1", 0.9), ("judge2", 0.9))
        candidate_err = MultiJudgeEvalRow(
            case_id="b",
            category="test",
            prompt="?",
            candidate_response="",
            candidate_error="http 500",
            verdicts=[],
        )
        result = _pair_agreement(
            [good, candidate_err],
            "judge1",
            "judge2",
            epsilon=0.1,
            pass_threshold=0.7,
        )
        assert result.n == 1


# --------------------------------------------------------------------------- #
# MultiJudgeRunner — construction                                              #
# --------------------------------------------------------------------------- #


class TestRunnerConstruction:
    def test_rejects_single_scorer(self) -> None:
        """For one judge, use EvalRunner. The multi-judge constructor
        refuses to silently accept a single-scorer config so callers
        notice they're on the wrong path."""
        with pytest.raises(ValueError, match="at least 2"):
            MultiJudgeRunner(
                candidate_base_url="http://x",
                candidate_api_key="k",
                candidate_model="m",
                scorers=[("only-one", StubScorer({}))],
            )

    def test_rejects_duplicate_judge_ids(self) -> None:
        with pytest.raises(ValueError, match="duplicate"):
            MultiJudgeRunner(
                candidate_base_url="http://x",
                candidate_api_key="k",
                candidate_model="m",
                scorers=[
                    ("same", StubScorer({})),
                    ("same", StubScorer({})),
                ],
            )


# --------------------------------------------------------------------------- #
# MultiJudgeRunner — end-to-end                                                #
# --------------------------------------------------------------------------- #


@respx.mock
@pytest.mark.asyncio
async def test_runner_fans_one_candidate_response_to_all_judges() -> None:
    """The candidate is called ONCE per case; the response goes to every
    judge. Two judges, two cases → 2 candidate calls + 4 judge calls.
    """
    candidate_route = respx.post(CANDIDATE_URL).mock(
        side_effect=[
            httpx.Response(200, json=_candidate_response("answer-a")),
            httpx.Response(200, json=_candidate_response("answer-b")),
        ]
    )
    gs = GoldenSet(
        name="t",
        cases=[
            EvalCase(id="a", category="x", prompt="?", expected="..."),
            EvalCase(id="b", category="x", prompt="?", expected="..."),
        ],
    )
    judge1 = StubScorer({"answer-a": 0.9, "answer-b": 0.5})
    judge2 = StubScorer({"answer-a": 0.85, "answer-b": 0.55})
    runner = MultiJudgeRunner(
        candidate_base_url="http://gateway.local",
        candidate_api_key="k",
        candidate_model="m",
        scorers=[("groq", judge1), ("anthropic", judge2)],
    )
    summary = await runner.run(gs)

    # Candidate called once per case, not per (case, judge).
    assert candidate_route.call_count == 2
    assert summary.total_cases == 2
    assert len(summary.rows) == 2
    # Each row has both judges' verdicts.
    for row in summary.rows:
        assert {v.judge_id for v in row.verdicts} == {"groq", "anthropic"}


@respx.mock
@pytest.mark.asyncio
async def test_runner_computes_pair_agreement() -> None:
    """End-to-end: scores 0.9 vs 0.85 and 0.5 vs 0.55 — both pairs are
    within ε=0.1, so within_epsilon_rate must be 1.0, mean Δ = 0.05."""
    respx.post(CANDIDATE_URL).mock(
        side_effect=[
            httpx.Response(200, json=_candidate_response("answer-a")),
            httpx.Response(200, json=_candidate_response("answer-b")),
        ]
    )
    gs = GoldenSet(
        name="t",
        cases=[
            EvalCase(id="a", category="x", prompt="?", expected="..."),
            EvalCase(id="b", category="x", prompt="?", expected="..."),
        ],
    )
    runner = MultiJudgeRunner(
        candidate_base_url="http://gateway.local",
        candidate_api_key="k",
        candidate_model="m",
        scorers=[
            ("groq", StubScorer({"answer-a": 0.9, "answer-b": 0.5})),
            ("anthropic", StubScorer({"answer-a": 0.85, "answer-b": 0.55})),
        ],
    )
    summary = await runner.run(gs)

    assert len(summary.pairs) == 1
    pair = summary.pairs[0]
    assert pair.n == 2
    assert pair.mean_abs_delta == pytest.approx(0.05)
    assert pair.within_epsilon_rate == 1.0


@respx.mock
@pytest.mark.asyncio
async def test_runner_handles_candidate_error_per_case() -> None:
    """When the candidate call fails for one case, that row carries a
    candidate_error and contributes zero verdicts. Other cases proceed."""
    respx.post(CANDIDATE_URL).mock(
        side_effect=[
            httpx.Response(200, json=_candidate_response("ok-1")),
            httpx.Response(500, text="boom"),
        ]
    )
    gs = GoldenSet(
        name="t",
        cases=[
            EvalCase(id="a", category="x", prompt="?", expected="..."),
            EvalCase(id="b", category="x", prompt="?", expected="..."),
        ],
    )
    runner = MultiJudgeRunner(
        candidate_base_url="http://gateway.local",
        candidate_api_key="k",
        candidate_model="m",
        scorers=[
            ("groq", StubScorer({"ok-1": 0.9})),
            ("anthropic", StubScorer({"ok-1": 0.85})),
        ],
    )
    summary = await runner.run(gs)

    assert summary.candidate_errors == 1
    # First row has both verdicts; second row has none and a candidate_error.
    assert len(summary.rows[0].verdicts) == 2
    assert summary.rows[1].candidate_error is not None
    assert len(summary.rows[1].verdicts) == 0
    # Pair agreement is computed over the 1 fully-scored row.
    assert summary.pairs[0].n == 1


@respx.mock
@pytest.mark.asyncio
async def test_runner_json_roundtrip() -> None:
    """``MultiJudgeEvalSummary.to_dict()`` must round-trip through json
    without losing any fields. Pinned because the schema is what the
    CI gate would diff against in a future phase."""
    import json

    respx.post(CANDIDATE_URL).mock(return_value=httpx.Response(200, json=_candidate_response("x")))
    gs = GoldenSet(name="t", cases=[EvalCase(id="a", category="x", prompt="?", expected="...")])
    runner = MultiJudgeRunner(
        candidate_base_url="http://gateway.local",
        candidate_api_key="k",
        candidate_model="m",
        scorers=[
            ("a", StubScorer({"x": 0.7})),
            ("b", StubScorer({"x": 0.8})),
        ],
    )
    summary = await runner.run(gs)
    blob = json.dumps(summary.to_dict())
    parsed = json.loads(blob)
    assert parsed["judges"] == ["a", "b"]
    assert len(parsed["rows"]) == 1
    assert len(parsed["pairs"]) == 1
