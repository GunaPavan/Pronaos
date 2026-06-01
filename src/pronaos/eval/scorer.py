"""LLM-as-judge scorer.

The judge is a configured chat model (Anthropic / Groq / OpenAI / etc.)
called through the same gateway as the candidate. We give it the
prompt, the expected-behaviour rubric, and the candidate's response,
and ask it to return a score 0.0-1.0 plus a one-sentence justification.

Why this design
---------------
- **Same gateway as production**: the judge call goes through your own
  ``/v1/chat/completions``. That means it's subject to your own
  guardrails, cache, observability — there's no second auth path or
  configuration surface to maintain.
- **Score plus justification**: the justification is dropped in run
  output so a human can spot-check borderline cases ("0.6: the response
  mentioned Paris but also incorrectly said it was on the Rhine"). This
  is what makes LLM-as-judge auditable rather than a black box.
- **Pinned prompt template**: the judge prompt is a hyperparameter just
  like the judge model. Both are recorded in run metadata so eval
  comparisons across runs are valid.

Pitfalls this avoids
--------------------
- Asking the judge to reply ONLY with a number: low-cost models often
  add prose anyway. We extract a float from anywhere in the response.
- Asking the judge to score 0-10 or 0-100: those granularities exceed
  what LLM judges can reliably distinguish. 0.0-1.0 with explicit
  half-stops (0.0/0.5/1.0) is the goldilocks zone.
- Trusting a single judge: parametrising the judge model lets you
  cross-check (Anthropic-judged vs Groq-judged) before trusting the
  numbers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

import httpx

# Tested prompt template — kept here so changes are version-controlled
# alongside the scoring logic.
_JUDGE_PROMPT_TEMPLATE = """You are an evaluation judge for an LLM gateway. Your job is to
score a candidate model's response against a human-authored rubric.

Score on a scale of 0.0 to 1.0:
- 1.0 — fully satisfies the rubric
- 0.5 — partially satisfies the rubric, with notable gaps or errors
- 0.0 — fails to satisfy the rubric

Reply with exactly two lines:
SCORE: <number between 0.0 and 1.0>
WHY: <one short sentence>

--- PROMPT ---
{prompt}

--- EXPECTED (rubric) ---
{expected}

--- CANDIDATE RESPONSE ---
{candidate}

Now produce your two-line verdict."""


@dataclass(frozen=True, slots=True)
class ScoreResult:
    """Outcome of one (case, candidate response) → judge call.

    ``raw`` carries the judge's full reply so a human can audit
    borderline calls. ``judge_error`` is set when the judge failed
    (network, parse error) — the runner treats this as an excluded
    sample rather than scoring it 0.0, because a busted judge would
    otherwise tank the aggregate."""

    score: float
    justification: str
    raw: str = ""
    judge_error: str | None = None

    @property
    def is_valid(self) -> bool:
        return self.judge_error is None


class Scorer(Protocol):
    """Stateless async scorer."""

    async def score(self, *, prompt: str, expected: str, candidate: str) -> ScoreResult: ...


class LLMJudgeScorer(Scorer):
    """Score by calling a chat model through the gateway.

    ``base_url`` + ``api_key`` point at any OpenAI-shape endpoint. In
    practice that's the gateway itself (``http://localhost:8080``), but
    you could point it at OpenAI's own API for an out-of-band sanity
    check that your gateway's judge call works identically.
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        judge_model: str,
        timeout_seconds: float = 30.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._judge_model = judge_model
        self._timeout = timeout_seconds

    async def score(self, *, prompt: str, expected: str, candidate: str) -> ScoreResult:
        body = _JUDGE_PROMPT_TEMPLATE.format(prompt=prompt, expected=expected, candidate=candidate)
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    f"{self._base_url}/v1/chat/completions",
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    json={
                        "model": self._judge_model,
                        "messages": [{"role": "user", "content": body}],
                        # Low temperature → consistent judging. We don't
                        # want creativity here.
                        "temperature": 0.0,
                        "max_tokens": 200,
                    },
                )
        except Exception as e:
            return ScoreResult(score=0.0, justification="", judge_error=f"network: {e}")

        if resp.status_code != 200:
            return ScoreResult(
                score=0.0,
                justification="",
                judge_error=f"http {resp.status_code}: {resp.text[:200]}",
            )

        text = resp.json()["choices"][0]["message"]["content"]
        return _parse_judge_reply(text)


# --------------------------------------------------------------------------- #
# Reply parsing                                                               #
# --------------------------------------------------------------------------- #


# Accept optional leading sign so out-of-range negative scores are
# parsed (and then clamped by the caller) rather than silently
# truncating to their absolute value.
_SCORE_RE = re.compile(r"SCORE\s*:\s*(-?[01](?:\.\d+)?|-?0?\.\d+)", re.IGNORECASE)
# Fallback: catches "0.7" anywhere in the reply if the SCORE label was
# dropped. Cheap-judge robustness. Stricter than the labeled form
# (no negative numbers in fallback) — a bare "-0.2" in prose is
# almost never an intended score and is more likely a noise token.
_BARE_FLOAT_RE = re.compile(r"\b([01](?:\.\d+)?|0?\.\d+)\b")
_WHY_RE = re.compile(r"WHY\s*:\s*(.+?)(?:\n|$)", re.IGNORECASE | re.DOTALL)


def _parse_judge_reply(text: str) -> ScoreResult:
    """Extract score + justification from a free-form judge reply.

    Defensive: cheap models occasionally ignore the format. We try the
    labeled form first, then fall back to "first plausible float in the
    response," then give up.
    """
    score_match = _SCORE_RE.search(text) or _BARE_FLOAT_RE.search(text)
    if score_match is None:
        return ScoreResult(
            score=0.0,
            justification="",
            raw=text,
            judge_error=f"could not parse score from reply: {text[:200]!r}",
        )
    try:
        score = float(score_match.group(1))
    except ValueError:
        return ScoreResult(
            score=0.0,
            justification="",
            raw=text,
            judge_error=f"unparseable score: {score_match.group(1)!r}",
        )
    # Clamp — defends against models hallucinating >1.0 or <0.0.
    score = max(0.0, min(1.0, score))

    why_match = _WHY_RE.search(text)
    justification = why_match.group(1).strip() if why_match else ""

    return ScoreResult(score=score, justification=justification, raw=text)
