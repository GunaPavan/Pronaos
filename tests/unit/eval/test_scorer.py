"""LLMJudgeScorer: prompt assembly + reply parsing + error handling.

We don't call a real LLM here — the scorer's HTTP call is mocked via
respx so tests stay hermetic and deterministic. The interesting logic
is parsing the judge's free-form reply into a score + justification,
and that's what we exercise here."""

from __future__ import annotations

import httpx
import pytest
import respx

from pronaos.eval.scorer import LLMJudgeScorer, ScoreResult, _parse_judge_reply

JUDGE_URL = "http://gateway.local/v1/chat/completions"


def _judge_reply(content: str) -> dict:
    """Build an OpenAI-shape chat response with the given content."""
    return {
        "id": "x",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}}],
    }


# --------------------------------------------------------------------------- #
# Parsing                                                                     #
# --------------------------------------------------------------------------- #


def test_parses_labelled_score_and_why() -> None:
    """Canonical reply shape — SCORE: + WHY: on separate lines."""
    r = _parse_judge_reply("SCORE: 0.85\nWHY: response correctly named Paris")
    assert r.score == pytest.approx(0.85)
    assert "Paris" in r.justification
    assert r.is_valid


def test_parses_score_when_judge_omits_why() -> None:
    """Some cheap judges drop the WHY line; the SCORE alone is enough.
    Justification ends up empty but the run still proceeds."""
    r = _parse_judge_reply("SCORE: 0.5")
    assert r.score == pytest.approx(0.5)
    assert r.justification == ""
    assert r.is_valid


def test_clamps_out_of_range_score() -> None:
    """Models occasionally output 1.5 or -0.2. Clamp rather than reject —
    the judge's intent is clear, the malformed value isn't."""
    high = _parse_judge_reply("SCORE: 1.5\nWHY: very good")
    assert high.score == 1.0
    low = _parse_judge_reply("SCORE: -0.2\nWHY: bad")
    assert low.score == 0.0


def test_falls_back_to_bare_float_when_label_missing() -> None:
    """Verbose judges sometimes write 'I'd give this a 0.7 because...'
    instead of using the SCORE: label. The fallback regex catches it."""
    r = _parse_judge_reply("Reasonable answer; I'd score this a 0.7 overall.")
    assert r.score == pytest.approx(0.7)
    assert r.is_valid


def test_unparseable_reply_marked_error() -> None:
    """If the judge's reply has no number at all, the run should mark
    this row as judge_error so it's excluded from aggregates rather
    than silently scored 0.0."""
    r = _parse_judge_reply("This is just text with no number anywhere.")
    assert r.judge_error is not None
    assert not r.is_valid


def test_score_case_insensitive() -> None:
    """Judges variously emit ``Score:``, ``SCORE:``, ``score :``. Match
    all of them."""
    r = _parse_judge_reply("score : 0.6\nwhy : ok")
    assert r.score == pytest.approx(0.6)


# --------------------------------------------------------------------------- #
# Scorer (HTTP) — mocked                                                      #
# --------------------------------------------------------------------------- #


@respx.mock
@pytest.mark.asyncio
async def test_scorer_returns_parsed_verdict() -> None:
    """End-to-end: call the gateway, get a reply, parse it."""
    respx.post(JUDGE_URL).mock(
        return_value=httpx.Response(
            200, json=_judge_reply("SCORE: 0.9\nWHY: very accurate")
        )
    )
    scorer = LLMJudgeScorer(
        base_url="http://gateway.local",
        api_key="pn_live_test",
        judge_model="anthropic/claude-haiku-4-5",
    )
    result: ScoreResult = await scorer.score(
        prompt="capital of france?", expected="say paris", candidate="Paris."
    )
    assert result.score == pytest.approx(0.9)
    assert result.is_valid
    assert "accurate" in result.justification


@respx.mock
@pytest.mark.asyncio
async def test_scorer_marks_judge_error_on_5xx() -> None:
    """A 5xx from the judge upstream must not be silently scored 0.0.
    The runner needs to know this row's score is missing, not bad."""
    respx.post(JUDGE_URL).mock(return_value=httpx.Response(503, text="provider down"))
    scorer = LLMJudgeScorer(
        base_url="http://gateway.local",
        api_key="pn_live_test",
        judge_model="anthropic/claude-haiku-4-5",
    )
    result = await scorer.score(prompt="q", expected="e", candidate="c")
    assert not result.is_valid
    assert "503" in result.judge_error


@respx.mock
@pytest.mark.asyncio
async def test_scorer_handles_network_error() -> None:
    """Connection drops / timeouts mark the row as judge_error too.
    Otherwise a flaky network would tank the aggregate score."""
    respx.post(JUDGE_URL).mock(side_effect=httpx.ConnectError("connection refused"))
    scorer = LLMJudgeScorer(
        base_url="http://gateway.local",
        api_key="pn_live_test",
        judge_model="anthropic/claude-haiku-4-5",
    )
    result = await scorer.score(prompt="q", expected="e", candidate="c")
    assert not result.is_valid
    assert "network" in result.judge_error
