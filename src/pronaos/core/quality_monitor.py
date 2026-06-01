"""Quality regression detection + automated re-routing (Phase 40).

Closed loop combining three earlier pieces:

- **Claim #10 LLM-judge scoring** — same ``LLMJudgeScorer`` that powers
  the eval framework grades production samples.
- **Claim #11 quality-aware routing** — the existing scorer reads
  ``team.quality_scores`` for per-model baselines and a
  ``quality_threshold`` floor. Phase 40 adds a *dynamic* signal layer
  on top: when monitoring detects a model has regressed below
  baseline, the scorer treats it as failing the threshold even if
  the stored baseline says otherwise.
- **Claim #16 Welch's t-test** — same statistical engine. We compare
  the model's recent N production samples to its baseline distribution
  and flip the degradation flag when the recent mean is significantly
  lower (default p < 0.05).

What this module owns
---------------------
- ``record_sample`` — append one (team, model, score, judge_model)
  row to ``quality_samples``. Pure I/O; the judge call is the
  caller's responsibility so the monitor stays sync-safe.
- ``check_degradation`` — pull recent N samples for (team, model)
  vs the baseline, run Welch's t-test, update
  ``teams.model_degradation_state``. Returns the transition that
  fired (``detected`` / ``recovered`` / ``no_change``) so the
  metric counter ticks correctly.
- ``is_degraded`` — boolean read of the team's per-model state.
  Used by the routing scorer to filter the candidate pool. Cheap
  (single JSON column read); the scorer is on the hot path and
  can't afford a DB hit per request, so we expect the scorer to
  receive the state dict as part of the principal/request context
  (already plumbed through for Phase 24 quality routing).

Sampling shape
--------------
Sampling is a per-response coin flip in the chat handler at rate
``team.quality_sampling_rate`` (default 0.0 = off). When the flip
hits, the handler fires an ``asyncio.create_task`` that:

1. Calls the judge model with (request, response) -> score.
2. Persists the score via ``record_sample``.
3. Calls ``check_degradation`` for the model that produced the
   response.

Step 3 is the cheap part — pulling ~25 recent samples and one t-test
is sub-100ms. Step 1 is the cost — a judge call. Operators tune
sampling_rate against their throughput and judge budget.

Fail-open semantics
-------------------
Sampling errors NEVER affect the client response. The whole pipeline
runs in a fire-and-forget background task, wrapped in broad
exception handling at every step. A judge outage = sampling gap +
log line; the gateway keeps serving requests at full speed.

Minimum-sample guards
---------------------
Two thresholds:

- ``min_recent_samples`` (default 10) — don't trust the t-test on
  fewer than this many recent samples. Premature degradation
  flagging on 3 bad samples in a row is the failure mode this
  prevents.
- ``min_baseline_samples`` (default 10) — same on the baseline
  side. Cold-start teams that haven't built up stored quality
  data skip the degradation check entirely (the scorer falls
  back to the regular quality-aware pool).

Why baseline_mean is read from ``team.quality_scores`` and not
recomputed from the samples table: the baseline is operator-curated
(via ``pronaos-cli eval store-scores``), reflects evaluated quality
on a known-good golden set, and shouldn't be polluted by production
sampling drift. Production sampling tracks deviation FROM that
baseline; it doesn't redefine it.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Final

from sqlalchemy import desc, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from pronaos.core.abtest_stats import welchs_t_test
from pronaos.db.models import QualitySample, Team
from pronaos.logging import get_logger

log = get_logger(__name__)

# Defaults. Mirror the documentation in the module docstring; if you
# change one, change the other.
DEFAULT_RECENT_WINDOW: Final = 25
DEFAULT_MIN_RECENT_SAMPLES: Final = 10
DEFAULT_P_VALUE_THRESHOLD: Final = 0.05
DEFAULT_RECOVERY_P_VALUE_THRESHOLD: Final = 0.10
# Recovery uses a looser p-value floor than detection. Rationale:
# you don't want a model to oscillate degraded/recovered/degraded
# every few samples around the threshold. Hysteresis: it takes
# strong evidence to degrade (p < 0.05), and weak evidence is
# enough to recover (p > 0.10).


class TransitionKind(StrEnum):
    """What ``check_degradation`` decided on this call."""

    DETECTED = "detected"
    RECOVERED = "recovered"
    NO_CHANGE = "no_change"


@dataclass(frozen=True, slots=True)
class DegradationCheck:
    """Outcome of one ``check_degradation`` invocation.

    ``transition`` reports whether anything CHANGED on the team's
    state — operators alert on ``detected`` and route metrics off
    the ``recovered`` transition. ``recent_mean`` and ``p_value``
    are returned for logging + dashboards regardless of transition.
    """

    transition: TransitionKind
    recent_mean: float
    baseline_mean: float
    n_recent: int
    p_value: float | None


# --------------------------------------------------------------------------- #
# Sample persistence                                                          #
# --------------------------------------------------------------------------- #


async def record_sample(
    session: AsyncSession,
    *,
    tenant_id: str,
    team_id: str,
    model: str,
    score: float,
    judge_model: str,
    request_id: str | None = None,
) -> QualitySample | None:
    """Append one quality_samples row. Fail-open on error.

    Score is clipped to [0, 1] before insert — defensive against a
    judge that returns an out-of-range value (the eval scorer's own
    contract is [0, 1] but we don't trust upstream contracts).
    """
    try:
        clipped = max(0.0, min(1.0, score))
        row = QualitySample(
            tenant_id=tenant_id,
            team_id=team_id,
            model=model,
            score=clipped,
            judge_model=judge_model,
            request_id=request_id,
        )
        session.add(row)
        await session.flush()
        return row
    except Exception as e:
        log.warning(
            "quality_monitor.record_sample_failed",
            team_id=team_id,
            model=model,
            error=str(e),
        )
        return None


# --------------------------------------------------------------------------- #
# Degradation check                                                           #
# --------------------------------------------------------------------------- #


async def check_degradation(
    session: AsyncSession,
    *,
    team_id: str,
    model: str,
    window: int = DEFAULT_RECENT_WINDOW,
    min_recent: int = DEFAULT_MIN_RECENT_SAMPLES,
    detect_p: float = DEFAULT_P_VALUE_THRESHOLD,
    recover_p: float = DEFAULT_RECOVERY_P_VALUE_THRESHOLD,
) -> DegradationCheck | None:
    """Pull recent samples + baseline; run Welch's t-test; update state.

    Returns ``None`` when the team or its baseline is missing — the
    caller treats that as "no monitoring active for this model"
    and moves on. Returns a ``DegradationCheck`` when a check ran,
    with ``transition`` reflecting the state-change verdict.

    Hysteresis: detection requires p < ``detect_p`` (default 0.05).
    Recovery requires p > ``recover_p`` (default 0.10). The gap
    prevents oscillation around the threshold; a model has to be
    materially better to be considered recovered, not just
    marginally less bad.
    """
    # ---- Step 1: load the team and its baseline for this model ----
    team = await session.get(Team, team_id)
    if team is None:
        return None
    baseline_entry = _baseline_score_for(team, model)
    if baseline_entry is None:
        # No baseline = nothing to compare against. The scorer's
        # fallback path (no quality_scores entry) takes over.
        return None
    baseline_mean = baseline_entry["score"]
    baseline_samples = baseline_entry.get("samples")  # list[float] or None

    # ---- Step 2: pull recent samples for this team+model ----
    recent_scores = await _fetch_recent_scores(
        session, team_id=team_id, model=model, limit=window
    )
    n_recent = len(recent_scores)
    if n_recent < min_recent:
        # Not enough recent data to trust the t-test. Don't change
        # state; return the current numbers for logging.
        recent_mean = statistics.fmean(recent_scores) if recent_scores else 0.0
        return DegradationCheck(
            transition=TransitionKind.NO_CHANGE,
            recent_mean=recent_mean,
            baseline_mean=baseline_mean,
            n_recent=n_recent,
            p_value=None,
        )
    recent_mean = statistics.fmean(recent_scores)

    # ---- Step 3: build a comparison sample for the baseline ----
    # Preferred: per-model baseline samples list. Fallback: synthesise
    # a constant-valued sample matching baseline_mean. The synthetic
    # path makes Welch's t-test less powerful (variance = 0 → SE
    # very small → t very large for small deltas), so operators
    # who care about precision should write a real samples list
    # via ``eval store-scores``.
    if (
        isinstance(baseline_samples, list)
        and len(baseline_samples) >= min_recent
        and all(isinstance(x, int | float) for x in baseline_samples)
    ):
        baseline_for_test = [float(x) for x in baseline_samples]
    else:
        baseline_for_test = [baseline_mean] * max(min_recent, n_recent)

    # ---- Step 4: Welch's t-test, BASELINE vs RECENT (one-sided) ----
    # We're testing the *one-sided* hypothesis "recent worse than
    # baseline." ``welchs_t_test`` returns two-sided p; we halve it
    # and only treat it as significant if mean_recent < mean_baseline.
    # When mean_recent >= mean_baseline, the model is FINE regardless
    # of p — recovery handling kicks in below.
    result = welchs_t_test(baseline_for_test, recent_scores)
    if result is None:
        return DegradationCheck(
            transition=TransitionKind.NO_CHANGE,
            recent_mean=recent_mean,
            baseline_mean=baseline_mean,
            n_recent=n_recent,
            p_value=None,
        )

    # Two-sided p halved when direction matches our hypothesis.
    one_sided_p = (
        (result.p_value / 2.0) if recent_mean < baseline_mean else 1.0
    )

    # ---- Step 5: decide the transition ----
    state = team.model_degradation_state or {}
    was_degraded = bool(state.get(model, {}).get("degraded", False))
    transition: TransitionKind

    if was_degraded:
        # Already degraded — look for recovery (recent NOT
        # significantly worse than baseline).
        if one_sided_p > recover_p:
            transition = TransitionKind.RECOVERED
        else:
            transition = TransitionKind.NO_CHANGE
    else:
        # Currently healthy — look for fresh degradation.
        if one_sided_p < detect_p and recent_mean < baseline_mean:
            transition = TransitionKind.DETECTED
        else:
            transition = TransitionKind.NO_CHANGE

    # ---- Step 6: persist state changes ----
    if transition != TransitionKind.NO_CHANGE:
        new_state = dict(state)
        if transition == TransitionKind.DETECTED:
            new_state[model] = {
                "degraded": True,
                "since_ts": datetime.now(tz=UTC).isoformat(),
                "baseline_mean": baseline_mean,
                "recent_mean": recent_mean,
                "n_recent": n_recent,
                "p_value": one_sided_p,
            }
        else:  # RECOVERED
            new_state[model] = {
                "degraded": False,
                "recovered_ts": datetime.now(tz=UTC).isoformat(),
                "baseline_mean": baseline_mean,
                "recent_mean": recent_mean,
                "n_recent": n_recent,
                "p_value": one_sided_p,
            }
        try:
            await session.execute(
                update(Team)
                .where(Team.id == team_id)
                .values(model_degradation_state=new_state)
            )
        except Exception as e:
            log.warning(
                "quality_monitor.state_update_failed",
                team_id=team_id,
                model=model,
                error=str(e),
            )
            # Still return the transition we WOULD have made — the
            # metric counter still ticks, operators see the event.

    return DegradationCheck(
        transition=transition,
        recent_mean=recent_mean,
        baseline_mean=baseline_mean,
        n_recent=n_recent,
        p_value=one_sided_p,
    )


# --------------------------------------------------------------------------- #
# Pure read helpers                                                           #
# --------------------------------------------------------------------------- #


def is_degraded(degradation_state: dict[str, Any] | None, model: str) -> bool:
    """Pure-function check: is ``model`` currently degraded per state?

    Used by the routing scorer on the hot path. The state dict is
    expected to already be in memory (loaded with the principal or
    the team row), so this is just a dict lookup — no I/O.
    """
    if not degradation_state:
        return False
    entry = degradation_state.get(model)
    if not isinstance(entry, dict):
        return False
    return bool(entry.get("degraded", False))


def degraded_models(degradation_state: dict[str, Any] | None) -> list[str]:
    """Return the list of currently-degraded fqmns for one team.

    Stable order (sorted) so response headers / log lines don't
    surface non-deterministic ordering across runs.
    """
    if not degradation_state:
        return []
    out: list[str] = []
    for fqmn, entry in degradation_state.items():
        if isinstance(entry, dict) and entry.get("degraded", False):
            out.append(fqmn)
    return sorted(out)


# --------------------------------------------------------------------------- #
# Internals                                                                   #
# --------------------------------------------------------------------------- #


def _baseline_score_for(team: Team, model: str) -> dict[str, Any] | None:
    """Resolve the baseline entry for ``model`` from team.quality_scores.

    Phase 24 stores ``team.quality_scores`` as
    ``{"fqmn": {"score": float, "n_samples": int, ...}}``. We accept
    that shape; missing entries or non-float scores return None.
    """
    scores = team.quality_scores
    if not isinstance(scores, dict):
        return None
    entry = scores.get(model)
    if not isinstance(entry, dict):
        return None
    raw = entry.get("score")
    if not isinstance(raw, int | float):
        return None
    return {
        "score": float(raw),
        "samples": entry.get("samples"),
    }


async def _fetch_recent_scores(
    session: AsyncSession, *, team_id: str, model: str, limit: int
) -> list[float]:
    """Pull the most-recent ``limit`` scores for (team, model), ordered ts desc.

    Returns the floats in insertion-order (newest first) — the
    t-test doesn't care about ordering but operators reading logs
    do. Index ``ix_quality_samples_team_model_ts`` makes this an
    O(log n + limit) operation regardless of total table size.
    """
    stmt = (
        select(QualitySample.score)
        .where(QualitySample.team_id == team_id, QualitySample.model == model)
        .order_by(desc(QualitySample.ts))
        .limit(limit)
    )
    result = await session.execute(stmt)
    return [float(row[0]) for row in result.all()]


# --------------------------------------------------------------------------- #
# Production judge                                                            #
# --------------------------------------------------------------------------- #


# Production sampling has no "expected" answer — we judge the response on
# intrinsic quality dimensions (coherence, helpfulness, factuality-by-
# self-consistency). The prompt is deliberately direct + asks for one
# float so the parser stays simple. Mirrors the eval scorer's
# instruction style for consistency across surfaces.
_PRODUCTION_JUDGE_PROMPT: Final = """\
You are evaluating an AI assistant's response to a user prompt for a \
quality-monitoring system. Score the response on a 0.0 to 1.0 scale \
based on how well it addresses the user's request:

- 1.0 = clearly correct, complete, well-formed
- 0.7 = adequate, minor issues
- 0.5 = partial answer, significant gaps
- 0.0 = clearly wrong, incoherent, or refuses without justification

Respond with EXACTLY one line in the format:
SCORE: <float>

USER PROMPT:
{prompt}

ASSISTANT RESPONSE:
{response}
"""


# Match SCORE: 0.83 / score: .7 / SCORE 0 / etc. Same lenience as the
# eval scorer's parser since cheap judges sometimes drop the colon.
_PRODUCTION_SCORE_RE: Final = __import__("re").compile(
    r"SCORE\s*:?\s*([01](?:\.\d+)?|0?\.\d+)", __import__("re").IGNORECASE
)


async def judge_response(
    *,
    base_url: str,
    api_key: str,
    judge_model: str,
    prompt: str,
    response: str,
    timeout_seconds: float = 10.0,
) -> float | None:
    """Call the judge model with (prompt, response) → score in [0, 1].

    Returns ``None`` on any failure (network, non-200, unparseable
    reply) — the caller (sampling background task) logs and skips
    persistence. We deliberately don't raise: this runs in a
    fire-and-forget task and an unhandled exception would just be
    swallowed by asyncio anyway.
    """
    # Local import keeps this module dependency-light and matches the
    # pattern used in eval/scorer.py (which is the only other place
    # we make outbound HTTP calls from core/eval code).
    import httpx

    judge_prompt = _PRODUCTION_JUDGE_PROMPT.format(prompt=prompt, response=response)
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            resp = await client.post(
                f"{base_url.rstrip('/')}/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": judge_model,
                    "messages": [{"role": "user", "content": judge_prompt}],
                    "temperature": 0.0,
                    "max_tokens": 30,
                },
            )
    except Exception as e:
        log.warning("quality_monitor.judge_call_failed", error=str(e))
        return None

    if resp.status_code != 200:
        log.warning(
            "quality_monitor.judge_non_200",
            status=resp.status_code,
            body_prefix=resp.text[:200],
        )
        return None

    try:
        text = resp.json()["choices"][0]["message"]["content"]
    except (KeyError, ValueError, IndexError):
        return None

    m = _PRODUCTION_SCORE_RE.search(text)
    if m is None:
        return None
    try:
        return max(0.0, min(1.0, float(m.group(1))))
    except ValueError:
        return None


__all__ = [
    "DEFAULT_MIN_RECENT_SAMPLES",
    "DEFAULT_P_VALUE_THRESHOLD",
    "DEFAULT_RECENT_WINDOW",
    "DegradationCheck",
    "TransitionKind",
    "check_degradation",
    "degraded_models",
    "is_degraded",
    "judge_response",
    "record_sample",
]
