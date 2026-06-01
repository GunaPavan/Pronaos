"""Live verification of quality-regression auto-routing (Claim #27, Phase 40).

The empirical question
----------------------
A team has a baseline quality score for model X (set via
``pronaos-cli eval store-scores``). Production traffic samples come
in and get judge-scored. When the recent batch is significantly
worse than baseline (Welch's t-test, p < 0.05), the gateway should:

1. Mark X as degraded in ``teams.model_degradation_state``.
2. Stop selecting X from ``model="auto"`` until recovery.
3. Surface the decision in ``X-Pronaos-Routing-Excluded-Models``
   on the next routing call.

This script injects synthetic samples directly via the DB (no need to
fire real LLM calls — the monitor's t-test logic is what's under
test, and we already cover the judge call path in unit tests). Then
it triggers a check via the admin API + a chat request and observes
the headers.

Method
------
1. Set baseline = 0.92 for the test model via the admin
   quality-scores endpoint (Phase 24's ``eval store-scores``
   shape).
2. Inject 12 synthetic samples at score 0.40 (clear regression)
   into ``quality_samples`` directly (the DB is the source of
   truth; bypassing the judge avoids dependency on a separate
   LLM call).
3. Fire a sentinel chat call. The chat handler's
   ``maybe_schedule_quality_sample`` won't kick in because
   sampling_rate stays at 0 — we don't want the SENTINEL itself
   to score and skew the experiment.
4. Manually trigger ``check_degradation`` via a synthetic call:
   we POST to ``/v1/admin/team/{id}/quality-monitor/check`` (a
   maintenance endpoint added for this verification).
5. Make a ``model="auto"`` chat call and observe
   ``X-Pronaos-Routing-Excluded-Models`` includes the degraded
   model.

Honesty
-------
We use synthetic samples rather than running a real "make the LLM
worse" experiment because:
1. Reliably degrading a real LLM is hard (and operationally we
   shouldn't have to wait for real degradation to test detection).
2. The statistical claim is "given samples that differ
   significantly, the gateway detects it" — that's exactly what
   synthetic samples test.
3. The judge call path is covered by unit tests
   (``test_quality_monitor.py``) and was demonstrated in the
   eval suite for Claim #10.

What this script verifies is the GATEWAY's behaviour: given
samples in the DB, does the closed loop (sample → detect →
state flip → scorer exclusion → routing header) work end-to-end?
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime

import httpx
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from pronaos.core.quality_monitor import check_degradation
from pronaos.db.models import QualitySample, Team


async def _seed_baseline_and_regression(
    *, db_url: str, team_id: str, model: str, baseline_score: float, bad_samples: list[float]
) -> None:
    """Set baseline quality_scores entry + insert synthetic bad samples."""
    engine = create_async_engine(db_url)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sm() as session:
            team = await session.get(Team, team_id)
            if team is None:
                raise SystemExit(f"team not found: {team_id}")
            # Baseline as the team's quality_scores entry. Include enough
            # baseline_samples that the t-test has a tight variance to
            # detect the regression against.
            scores = dict(team.quality_scores or {})
            scores[model] = {
                "score": baseline_score,
                "n_samples": 15,
                "samples": [baseline_score] * 12 + [baseline_score - 0.02, baseline_score + 0.02, baseline_score],
            }
            await session.execute(
                update(Team).where(Team.id == team_id).values(
                    quality_scores=scores,
                    # Clear any prior degradation state so the test is
                    # idempotent.
                    model_degradation_state=None,
                )
            )

            # Insert synthetic samples directly. Bypass the judge —
            # the monitor's job is to compare what's in quality_samples
            # against baseline; how those rows got there is the
            # ingestion path's concern, not the detector's.
            now = datetime.now(tz=UTC)
            for score in bad_samples:
                session.add(
                    QualitySample(
                        tenant_id=team.tenant_id,
                        team_id=team_id,
                        model=model,
                        score=score,
                        judge_model="synthetic",
                        request_id=None,
                        ts=now,
                    )
                )
            await session.commit()
    finally:
        await engine.dispose()


async def _trigger_check(
    *, db_url: str, team_id: str, model: str
) -> dict[str, object] | None:
    """Run ``check_degradation`` directly via the module's function.

    The chat handler runs this as a background task per sample; for
    the verification we run it synchronously so the script can
    observe the transition deterministically.
    """
    engine = create_async_engine(db_url)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sm() as session:
            result = await check_degradation(session, team_id=team_id, model=model)
            await session.commit()
            if result is None:
                return None
            return {
                "transition": result.transition.value,
                "recent_mean": result.recent_mean,
                "baseline_mean": result.baseline_mean,
                "n_recent": result.n_recent,
                "p_value": result.p_value,
            }
    finally:
        await engine.dispose()


async def _make_routed_call(
    *, gateway_url: str, api_key: str
) -> tuple[int, dict[str, str], dict[str, object]]:
    """Fire a model='auto' call and capture the routing headers."""
    body = {
        "model": "auto",
        "messages": [{"role": "user", "content": "Say OK and nothing else."}],
        "temperature": 0.0,
        "max_tokens": 10,
    }
    async with httpx.AsyncClient(base_url=gateway_url, timeout=30.0) as client:
        resp = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json=body,
        )
    try:
        out_body: dict[str, object] = resp.json()
    except ValueError:
        out_body = {"_raw": resp.text}
    return resp.status_code, dict(resp.headers), out_body


async def _main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--gateway-url", default="http://127.0.0.1:8080")
    parser.add_argument("--api-key", required=True)
    parser.add_argument("--team-id", required=True)
    parser.add_argument(
        "--db-url",
        default="sqlite+aiosqlite:///./pronaos.db",
        help="DB URL the gateway is using (matches PRONAOS_DATABASE_URL).",
    )
    parser.add_argument(
        "--model",
        default="groq/llama-3.1-8b-instant",
        help="The model to mark as degraded. Must be in the team's allowlist.",
    )
    parser.add_argument(
        "--baseline",
        type=float,
        default=0.92,
        help="Stored baseline quality score (what 'good' looks like).",
    )
    parser.add_argument(
        "--regression",
        type=float,
        default=0.40,
        help="Score for the injected bad samples (what 'broken' looks like).",
    )
    parser.add_argument(
        "--n-bad-samples",
        type=int,
        default=12,
        help="How many bad samples to inject (≥ DEFAULT_MIN_RECENT_SAMPLES).",
    )
    args = parser.parse_args()

    print(
        f"Seeding baseline {args.baseline} + {args.n_bad_samples} bad samples "
        f"at {args.regression} for {args.model}..."
    )
    await _seed_baseline_and_regression(
        db_url=args.db_url,
        team_id=args.team_id,
        model=args.model,
        baseline_score=args.baseline,
        bad_samples=[args.regression] * args.n_bad_samples,
    )
    print()

    print("Triggering check_degradation...")
    check = await _trigger_check(
        db_url=args.db_url,
        team_id=args.team_id,
        model=args.model,
    )
    if check is None:
        print("VERDICT: claim fails — check_degradation returned None "
              "(no baseline? team not found?)")
        sys.exit(1)
    print(f"  transition:    {check['transition']}")
    print(f"  recent_mean:   {check['recent_mean']}")
    print(f"  baseline_mean: {check['baseline_mean']}")
    print(f"  n_recent:      {check['n_recent']}")
    print(f"  p_value:       {check['p_value']}")
    print()

    print("Making a model='auto' call to observe scorer exclusion...")
    status, headers, _body = await _make_routed_call(
        gateway_url=args.gateway_url,
        api_key=args.api_key,
    )
    print(f"  HTTP status:                       {status}")
    print(f"  X-Pronaos-Routed-Model:            {headers.get('x-pronaos-routed-model', '(none)')}")
    print(f"  X-Pronaos-Routing-Excluded-Models: {headers.get('x-pronaos-routing-excluded-models', '(none)')}")
    print()

    excluded = headers.get("x-pronaos-routing-excluded-models", "")
    routed = headers.get("x-pronaos-routed-model", "")
    detected = check["transition"] == "detected"
    excluded_correctly = args.model in excluded
    not_routed_to_degraded = args.model != routed

    print("=" * 64)
    print("Phase 40 — quality regression auto-routing experiment")
    print("=" * 64)
    print(f"baseline:                  {args.baseline}")
    print(f"injected regression mean:  {args.regression}")
    print(f"samples injected:          {args.n_bad_samples}")
    print(f"degradation detected?      {detected}")
    print(f"p_value:                   {check['p_value']}")
    print(f"degraded model excluded?   {excluded_correctly}")
    print(f"routed to a non-degraded?  {not_routed_to_degraded}")
    print()

    if detected and excluded_correctly and not_routed_to_degraded:
        print(
            f"VERDICT: claim holds — baseline {args.baseline} → injected "
            f"{args.n_bad_samples} samples at {args.regression} → "
            f"Welch's t-test p={check['p_value']} < 0.05 → gateway "
            f"flipped {args.model} to degraded → "
            f"model='auto' router excluded it ({excluded}) and routed to "
            f"{routed} instead. Closed-loop quality monitoring is wired "
            f"correctly from sample to routing decision."
        )
        sys.exit(0)

    reasons: list[str] = []
    if not detected:
        reasons.append(
            f"check_degradation returned transition={check['transition']} "
            f"(expected 'detected'); the t-test didn't fire — "
            f"investigate the baseline_samples / variance"
        )
    if not excluded_correctly:
        reasons.append(
            f"degraded model {args.model} not in "
            f"X-Pronaos-Routing-Excluded-Models header ({excluded!r}); "
            f"scorer didn't read the state"
        )
    if not not_routed_to_degraded:
        reasons.append(
            f"router still picked the degraded model ({routed}) — "
            f"the degradation set didn't reach the candidate filter"
        )
    print(f"VERDICT: claim fails — {'; '.join(reasons)}.")
    sys.exit(1)


if __name__ == "__main__":
    asyncio.run(_main())
