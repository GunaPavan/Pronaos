"""Cache-quality experiment.

Question: does the semantic cache compromise answer quality?

Method
------
1. Clear Redis + Qdrant.
2. Run the eval suite — every request is a cache miss; record scores.
3. Run the same suite again — every request is a cache hit (L1 or L2);
   record scores.
4. Compute per-case score delta and aggregate.

Claim
-----
If the gateway's caching layer is correct, the second-run scores must
equal the first-run scores (responses are byte-identical, judge is
temperature=0 so deterministic). Any per-case delta > epsilon is a real
correctness bug — a quiet response-shape mutation in the cache path, an
encoding bug, header leakage into the cached body, etc.

This is the obvious-but-rarely-verified property production systems
break in subtle ways. Running it gives you a publishable empirical
claim about your gateway.

Future extensions (Phase 9.2+)
------------------------------
- Paraphrase experiment: a paraphrased golden set with no L1 cache hits
  but high L2 semantic hits. Tests the harder claim: do *semantically*
  matched cached responses still answer the new question correctly?
- Guardrail-quality experiment: redaction-on vs redaction-off, same
  prompts. Does redaction degrade scores?
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import httpx

from pronaos.eval.data import load_golden_set
from pronaos.eval.runner import EvalRunner, EvalRunSummary
from pronaos.eval.scorer import LLMJudgeScorer

# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _clear_redis() -> bool:
    """Use docker to FLUSHALL the dev Redis container.

    Returns True on success, False if Redis can't be reached — in which
    case the experiment falls back to "run twice without clearing" which
    still produces interpretable results (run 1 may be cache hits from
    prior traffic; run 2 = cache hits regardless).
    """
    if shutil.which("docker") is None:
        return False
    try:
        result = subprocess.run(
            ["docker", "exec", "pronaos-redis-1", "redis-cli", "FLUSHALL"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


async def _clear_qdrant(qdrant_url: str = "http://localhost:6333") -> bool:
    """Drop the semantic-cache collection. Gateway re-creates it on
    next put (modulo a restart of the gateway — see note below)."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.delete(
                f"{qdrant_url}/collections/pronaos_semantic_cache"
            )
            return resp.status_code in (200, 404)
    except Exception:
        return False


async def _wait_for_gateway(base_url: str, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    async with httpx.AsyncClient(timeout=2.0) as client:
        while time.monotonic() < deadline:
            try:
                resp = await client.get(f"{base_url}/v1/healthz")
                if resp.status_code == 200:
                    return True
            except Exception:
                pass
            await asyncio.sleep(1.0)
    return False


# --------------------------------------------------------------------------- #
# Experiment                                                                  #
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class CompareRow:
    case_id: str
    category: str
    fresh_score: float
    cached_score: float

    @property
    def delta(self) -> float:
        return self.cached_score - self.fresh_score


async def run_experiment(
    *,
    base_url: str,
    api_key: str,
    candidate_model: str,
    judge_model: str,
    golden_set_path: Path,
    epsilon: float,
    output_json: Path | None,
) -> int:
    golden_set = load_golden_set(golden_set_path)

    print("Pronaos cache-quality experiment")
    print("=" * 60)
    print(f"golden set:  {golden_set.name} ({len(golden_set)} cases)")
    print(f"candidate:   {candidate_model}")
    print(f"judge:       {judge_model}")
    print(f"epsilon:     {epsilon}")
    print()

    # Step 1 — clear caches.
    print("[1/3] clearing Redis + Qdrant caches...")
    redis_ok = _clear_redis()
    qdrant_ok = await _clear_qdrant()
    print(f"      redis cleared: {redis_ok}")
    print(f"      qdrant cleared: {qdrant_ok}")
    if not (redis_ok and qdrant_ok):
        print("      ⚠ partial clear — experiment still runs, baseline may "
              "include warm hits.")

    # Note: deleting the Qdrant collection means the gateway's L2 cache
    # will fail to upsert on the FIRST run (collection doesn't exist).
    # The gateway re-creates the collection only at startup. So the L2
    # side of the experiment only has data on the SECOND run.
    # For this first-pass experiment that's acceptable: L1 (Redis) is
    # the dominant cache anyway, and the property we're verifying
    # (cached scores equal fresh scores) is testable with L1 alone.
    print("      note: L2 only re-populates after the gateway restarts;")
    print("            L1 (Redis exact-match) carries the full experiment.")
    print()

    scorer = LLMJudgeScorer(base_url=base_url, api_key=api_key, judge_model=judge_model)
    runner = EvalRunner(
        candidate_base_url=base_url,
        candidate_api_key=api_key,
        candidate_model=candidate_model,
        scorer=scorer,
        judge_model_id=judge_model,
    )

    # Step 2 — fresh run (every request misses cache).
    print(f"[2/3] fresh run ({len(golden_set)} cases, every request → provider)...")
    t0 = time.monotonic()
    fresh: EvalRunSummary = await runner.run(golden_set)
    fresh_dt = time.monotonic() - t0
    print(f"      mean: {fresh.overall_mean:.3f}  scored: {fresh.scored_cases}/{fresh.total_cases}  "
          f"wall: {fresh_dt:.1f}s")
    print()

    # Step 3 — cached run (L1 should hit every request).
    print(f"[3/3] cached run ({len(golden_set)} cases, expecting L1 hits)...")
    t1 = time.monotonic()
    cached: EvalRunSummary = await runner.run(golden_set)
    cached_dt = time.monotonic() - t1
    print(f"      mean: {cached.overall_mean:.3f}  scored: {cached.scored_cases}/{cached.total_cases}  "
          f"wall: {cached_dt:.1f}s")
    print()

    # Compare.
    fresh_by_id = {r.case_id: r for r in fresh.rows if r.is_scored}
    cached_by_id = {r.case_id: r for r in cached.rows if r.is_scored}
    common = sorted(set(fresh_by_id) & set(cached_by_id))

    rows: list[CompareRow] = [
        CompareRow(
            case_id=cid,
            category=fresh_by_id[cid].category,
            fresh_score=fresh_by_id[cid].score,
            cached_score=cached_by_id[cid].score,
        )
        for cid in common
    ]

    deltas = [r.delta for r in rows]
    mean_delta = sum(deltas) / len(deltas) if deltas else 0.0
    max_abs_delta = max(abs(d) for d in deltas) if deltas else 0.0
    over_eps = [r for r in rows if abs(r.delta) > epsilon]

    print("=" * 60)
    print("PER-CASE SCORES")
    print(f"  {'case':<28} {'cat':<14} {'fresh':>6} {'cached':>7} {'Δ':>6}")
    for r in rows:
        flag = "  ⚠" if abs(r.delta) > epsilon else ""
        print(
            f"  {r.case_id:<28} {r.category:<14} "
            f"{r.fresh_score:>6.2f} {r.cached_score:>7.2f} {r.delta:>+6.2f}{flag}"
        )

    print()
    print("=" * 60)
    print("AGGREGATE")
    print(f"  cases compared:   {len(rows)}")
    print(f"  fresh mean:       {fresh.overall_mean:.3f}")
    print(f"  cached mean:      {cached.overall_mean:.3f}")
    print(f"  mean Δ:           {mean_delta:+.4f}")
    print(f"  max |Δ|:          {max_abs_delta:.4f}")
    print(f"  cases over ε:     {len(over_eps)} / {len(rows)}")
    print()

    if not rows:
        print("INCONCLUSIVE: no cases scored cleanly in both runs.")
        return 2

    if max_abs_delta <= epsilon:
        print(
            f"✅ CLAIM HOLDS: cache preserves quality.\n"
            f"   Max per-case score difference of {max_abs_delta:.4f} ≤ "
            f"ε={epsilon}.\n"
            f"   Across {len(rows)} cases, no regression detected."
        )
        exit_code = 0
    else:
        print(
            f"❌ REGRESSION: {len(over_eps)} case(s) shifted by more than "
            f"ε={epsilon}.\n"
            f"   Max |Δ| = {max_abs_delta:.4f}."
        )
        exit_code = 1

    # Save the comparison for archival / README embedding.
    if output_json is not None:
        import json

        output_json.write_text(
            json.dumps(
                {
                    "golden_set": golden_set.name,
                    "candidate_model": candidate_model,
                    "judge_model": judge_model,
                    "epsilon": epsilon,
                    "fresh_mean": fresh.overall_mean,
                    "cached_mean": cached.overall_mean,
                    "mean_delta": mean_delta,
                    "max_abs_delta": max_abs_delta,
                    "cases_over_epsilon": len(over_eps),
                    "rows": [
                        {
                            "case_id": r.case_id,
                            "category": r.category,
                            "fresh_score": r.fresh_score,
                            "cached_score": r.cached_score,
                            "delta": r.delta,
                        }
                        for r in rows
                    ],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        print()
        print(f"saved: {output_json}")

    return exit_code


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Run the eval suite twice (cache cleared then warm) and verify "
            "the cache doesn't compromise answer quality."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--base-url", default="http://localhost:8080")
    p.add_argument(
        "--api-key",
        default=os.environ.get("PRONAOS_DEMO_API_KEY"),
        help="API key (or set PRONAOS_DEMO_API_KEY).",
    )
    p.add_argument(
        "--candidate-model", default="groq/llama-3.1-8b-instant"
    )
    p.add_argument("--judge-model", default="groq/llama-3.3-70b-versatile")
    p.add_argument(
        "--golden-set",
        type=Path,
        default=Path("tests/eval/data/basic.yaml"),
    )
    p.add_argument(
        "--epsilon",
        type=float,
        default=0.05,
        help="Max acceptable per-case score difference. Default 0.05 "
        "absorbs judge stochasticity; tighten in CI.",
    )
    p.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Save the comparison JSON to this path.",
    )
    return p.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.api_key:
        print(
            "error: --api-key is required (or set PRONAOS_DEMO_API_KEY).",
            file=sys.stderr,
        )
        return 2

    try:
        return asyncio.run(
            run_experiment(
                base_url=args.base_url,
                api_key=args.api_key,
                candidate_model=args.candidate_model,
                judge_model=args.judge_model,
                golden_set_path=args.golden_set,
                epsilon=args.epsilon,
                output_json=args.output,
            )
        )
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
