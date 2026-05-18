"""Paraphrase-cache quality experiment.

The L1 cache-quality experiment (``eval_cache_quality.py``) verified the
*boring* claim: the cache returns byte-identical responses when asked
the exact same question twice.

This experiment verifies the **interesting** claim:

    When the user asks a SEMANTICALLY EQUIVALENT BUT DIFFERENT prompt,
    the L2 (Qdrant + embeddings) cache serves a cached response — and
    that response is still good enough to score against the rubric.

Three outcomes are publishable
------------------------------

1. **High L2 hit rate, Δscore ≈ 0**: cache is semantically aware AND
   quality-preserving. The headline result if it lands.

2. **Low L2 hit rate, Δscore ≈ 0**: cache is conservative (high
   similarity threshold). Paraphrases that humans consider equivalent
   often don't reach cosine 0.95 with all-MiniLM-L6-v2. Useful tuning
   insight: "lower the threshold to claw back hits."

3. **High L2 hit rate, Δscore > 0**: cache trades quality for hits at
   the current threshold. The cost number for the README:
   "X% hit rate costs Y% answer quality."

All three are real claims. There's no "boring outcome" because the
question hasn't been asked of this gateway before.

Method
------
1. Clear Redis + Qdrant.
2. RESTART gateway needed so the Qdrant collection is recreated (the
   gateway's ensure_ready runs at startup). The script doesn't restart
   automatically — it expects you to have just done so.
3. Prime: send each ``basic.yaml`` prompt → fill cache + capture
   ``fresh score`` baseline.
4. Send each ``basic_paraphrased.yaml`` prompt → record cache tier
   (from ``X-Pronaos-Cache`` header) + paraphrased score.
5. Match by case id. Report:
     - L2 hit rate
     - Per-case Δscore (paraphrased − fresh)
     - Aggregate mean/max Δ
     - Verdict
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from pronaos.eval.data import EvalCase, load_golden_set
from pronaos.eval.scorer import LLMJudgeScorer, ScoreResult

# --------------------------------------------------------------------------- #
# Cache clearing                                                              #
# --------------------------------------------------------------------------- #


def _clear_redis() -> bool:
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
    """Empty the semantic-cache collection's POINTS, not the collection
    itself. Dropping the collection would force a gateway restart
    (``ensure_ready`` only runs at startup); just emptying the points
    leaves the schema in place so the gateway can immediately write to it."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            # Match-all filter via the POST /points/delete endpoint.
            # An empty ``filter.must`` means "delete every point" — Qdrant
            # treats that as a full clear, equivalent to TRUNCATE.
            resp = await client.post(
                f"{qdrant_url}/collections/pronaos_semantic_cache/points/delete",
                json={"filter": {"must": []}},
            )
            return resp.status_code in (200, 202)
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# Per-case runner that captures the cache tier                                #
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class CaseResult:
    """Outcome of one case: candidate response, judge verdict, cache tier."""

    case_id: str
    category: str
    response: str
    score: float
    justification: str
    cache_header: str = ""
    error: str | None = None

    @property
    def cache_tier(self) -> str:
        """Parse X-Pronaos-Cache → one of: miss | exact | semantic | skip | none."""
        h = self.cache_header.lower()
        if h.startswith("hit:semantic"):
            return "semantic"
        if h.startswith("hit:"):
            return "exact"
        if h == "skip":
            return "skip"
        if h == "miss":
            return "miss"
        return "none"


async def _fire_case(
    client: httpx.AsyncClient,
    *,
    api_key: str,
    candidate_model: str,
    case: EvalCase,
    scorer: LLMJudgeScorer,
) -> CaseResult:
    try:
        resp = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": candidate_model,
                "messages": [{"role": "user", "content": case.prompt}],
                "temperature": 0.0,
                "max_tokens": 400,
            },
            timeout=30.0,
        )
    except Exception as e:
        return CaseResult(
            case_id=case.id,
            category=case.category,
            response="",
            score=0.0,
            justification="",
            error=f"network: {e}",
        )

    if resp.status_code != 200:
        return CaseResult(
            case_id=case.id,
            category=case.category,
            response="",
            score=0.0,
            justification="",
            error=f"http {resp.status_code}: {resp.text[:160]}",
        )

    text = resp.json()["choices"][0]["message"]["content"]
    header = resp.headers.get("x-pronaos-cache", "")

    verdict: ScoreResult = await scorer.score(
        prompt=case.prompt, expected=case.expected, candidate=text
    )

    return CaseResult(
        case_id=case.id,
        category=case.category,
        response=text,
        score=verdict.score,
        justification=verdict.justification,
        cache_header=header,
        error=verdict.judge_error,
    )


# --------------------------------------------------------------------------- #
# Experiment                                                                  #
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class CompareRow:
    case_id: str
    category: str
    paraphrase: str
    fresh_score: float
    paraphrased_score: float
    paraphrased_tier: str

    @property
    def delta(self) -> float:
        return self.paraphrased_score - self.fresh_score


@dataclass(slots=True)
class ExperimentResult:
    rows: list[CompareRow] = field(default_factory=list)
    l2_hits: int = 0
    l1_hits: int = 0
    misses: int = 0


async def run_experiment(
    *,
    base_url: str,
    api_key: str,
    candidate_model: str,
    judge_model: str,
    basic_path: Path,
    paraphrased_path: Path,
    epsilon: float,
    output_json: Path | None,
) -> int:
    basic = load_golden_set(basic_path)
    paraphrased = load_golden_set(paraphrased_path)

    # Sanity-check matched IDs — if the user edits one set but not the other,
    # the per-case diff becomes meaningless.
    basic_ids = {c.id for c in basic.cases}
    para_ids = {c.id for c in paraphrased.cases}
    if basic_ids != para_ids:
        print(
            f"error: case-id mismatch. basic-only={basic_ids - para_ids}, "
            f"paraphrased-only={para_ids - basic_ids}",
            file=sys.stderr,
        )
        return 2

    print("Pronaos paraphrase-cache quality experiment")
    print("=" * 70)
    print(f"basic set:        {basic.name} ({len(basic)} cases)")
    print(f"paraphrased set:  {paraphrased.name} ({len(paraphrased)} cases)")
    print(f"candidate:        {candidate_model}")
    print(f"judge:            {judge_model}")
    print(f"epsilon:          {epsilon}")
    print()

    # 1. Clear caches.
    print("[1/3] clearing Redis + Qdrant caches...")
    redis_ok = _clear_redis()
    qdrant_ok = await _clear_qdrant()
    print(f"      redis cleared: {redis_ok}")
    print(f"      qdrant cleared: {qdrant_ok}")
    if not (redis_ok and qdrant_ok):
        print("      ⚠ partial clear — experiment may include warm hits.")
    print(
        "      note: Qdrant collection is re-created on gateway startup.\n"
        "            For accurate L2 hit measurement you should have\n"
        "            restarted the gateway between deletes."
    )
    print()

    scorer = LLMJudgeScorer(
        base_url=base_url, api_key=api_key, judge_model=judge_model
    )

    # 2. Prime — populate cache + record fresh scores.
    print(
        f"[2/3] priming: send {len(basic)} basic prompts → cache fills, "
        f"scores recorded..."
    )
    t0 = time.monotonic()
    fresh_results: dict[str, CaseResult] = {}
    async with httpx.AsyncClient(base_url=base_url) as client:
        for case in basic.cases:
            r = await _fire_case(
                client,
                api_key=api_key,
                candidate_model=candidate_model,
                case=case,
                scorer=scorer,
            )
            fresh_results[case.id] = r
    fresh_dt = time.monotonic() - t0
    fresh_scored = sum(1 for r in fresh_results.values() if r.error is None)
    fresh_mean = (
        sum(r.score for r in fresh_results.values() if r.error is None) / fresh_scored
        if fresh_scored
        else 0.0
    )
    print(f"      scored: {fresh_scored}/{len(basic)}  mean: {fresh_mean:.3f}  wall: {fresh_dt:.1f}s")
    print()

    # 3. Run paraphrased prompts — these should L2-hit the cached entries.
    print(
        f"[3/3] running {len(paraphrased)} paraphrased prompts (expecting L2 hits)..."
    )
    t1 = time.monotonic()
    paraphrased_results: dict[str, CaseResult] = {}
    async with httpx.AsyncClient(base_url=base_url) as client:
        for case in paraphrased.cases:
            r = await _fire_case(
                client,
                api_key=api_key,
                candidate_model=candidate_model,
                case=case,
                scorer=scorer,
            )
            paraphrased_results[case.id] = r
    para_dt = time.monotonic() - t1
    print(f"      wall: {para_dt:.1f}s")
    print()

    # Build the comparison.
    paraphrased_by_id: dict[str, EvalCase] = {c.id: c for c in paraphrased.cases}
    exp = ExperimentResult()
    for cid in sorted(basic_ids):
        fresh = fresh_results[cid]
        para = paraphrased_results[cid]
        if fresh.error or para.error:
            continue
        tier = para.cache_tier
        if tier == "exact":
            exp.l1_hits += 1
        elif tier == "semantic":
            exp.l2_hits += 1
        elif tier == "miss":
            exp.misses += 1
        exp.rows.append(
            CompareRow(
                case_id=cid,
                category=fresh.category,
                paraphrase=paraphrased_by_id[cid].prompt,
                fresh_score=fresh.score,
                paraphrased_score=para.score,
                paraphrased_tier=tier,
            )
        )

    print("=" * 70)
    print("PER-CASE")
    print(
        f"  {'case':<22} {'fresh':>5} {'para':>5} {'Δ':>6}  {'tier':<8}  paraphrase"
    )
    for r in exp.rows:
        flag = " ⚠" if abs(r.delta) > epsilon else ""
        para_short = r.paraphrase if len(r.paraphrase) <= 36 else r.paraphrase[:33] + "…"
        print(
            f"  {r.case_id:<22} {r.fresh_score:>5.2f} {r.paraphrased_score:>5.2f} "
            f"{r.delta:>+6.2f}  {r.paraphrased_tier:<8}  {para_short}{flag}"
        )

    print()
    print("=" * 70)
    print("AGGREGATE")
    n = len(exp.rows)
    deltas = [r.delta for r in exp.rows]
    mean_delta = sum(deltas) / n if n else 0.0
    max_abs = max(abs(d) for d in deltas) if deltas else 0.0
    fresh_mean = sum(r.fresh_score for r in exp.rows) / n if n else 0.0
    para_mean = sum(r.paraphrased_score for r in exp.rows) / n if n else 0.0
    l2_hit_rate = exp.l2_hits / n if n else 0.0
    l1_hit_rate = exp.l1_hits / n if n else 0.0
    miss_rate = exp.misses / n if n else 0.0

    print(f"  cases:              {n}")
    print(f"  fresh mean:         {fresh_mean:.3f}")
    print(f"  paraphrased mean:   {para_mean:.3f}")
    print(f"  mean Δ:             {mean_delta:+.4f}")
    print(f"  max |Δ|:            {max_abs:.4f}")
    print()
    print("  paraphrased tier breakdown:")
    print(f"    L2 (semantic) hits: {exp.l2_hits} / {n} ({l2_hit_rate:.1%})")
    print(f"    L1 (exact) hits:    {exp.l1_hits} / {n} ({l1_hit_rate:.1%})")
    print(f"    misses:             {exp.misses} / {n} ({miss_rate:.1%})")
    print()

    # Verdict logic — three outcomes flagged.
    print("=" * 70)
    if exp.l2_hits == 0:
        print(
            "ℹ️  CONSERVATIVE THRESHOLD: zero L2 hits on these paraphrases.\n"
            f"   At similarity threshold 0.95, all-MiniLM-L6-v2 considered\n"
            f"   none of these {n} paraphrases close enough to their\n"
            f"   originals. The semantic cache is working safely — it isn't\n"
            f"   over-serving. To trade conservatism for hit rate, lower\n"
            f"   PRONAOS_SEMANTIC_CACHE_THRESHOLD."
        )
        verdict_code = "conservative"
        exit_code = 0
    elif max_abs <= epsilon:
        print(
            f"✅ CLAIM HOLDS: semantic cache preserves quality on paraphrases.\n"
            f"   L2 served {exp.l2_hits}/{n} cases ({l2_hit_rate:.1%}); on\n"
            f"   every L2-served case the paraphrased score was within\n"
            f"   ε={epsilon} of the fresh score.\n"
            f"   Headline: max per-case Δ = {max_abs:.4f}."
        )
        verdict_code = "preserves_quality"
        exit_code = 0
    else:
        print(
            f"⚠ QUALITY TRADE-OFF DETECTED.\n"
            f"   L2 hit rate: {l2_hit_rate:.1%}.\n"
            f"   But max per-case |Δ| = {max_abs:.4f} > ε = {epsilon}.\n"
            f"   The cache is serving paraphrases but losing answer quality\n"
            f"   on some of them. Real FinOps trade-off — consider raising\n"
            f"   the similarity threshold."
        )
        verdict_code = "quality_tradeoff"
        exit_code = 1

    if output_json is not None:
        output_json.parent.mkdir(parents=True, exist_ok=True)
        output_json.write_text(
            json.dumps(
                {
                    "verdict": verdict_code,
                    "candidate_model": candidate_model,
                    "judge_model": judge_model,
                    "epsilon": epsilon,
                    "cases": n,
                    "fresh_mean": fresh_mean,
                    "paraphrased_mean": para_mean,
                    "mean_delta": mean_delta,
                    "max_abs_delta": max_abs,
                    "l2_hit_rate": l2_hit_rate,
                    "l1_hit_rate": l1_hit_rate,
                    "miss_rate": miss_rate,
                    "rows": [
                        {
                            "case_id": r.case_id,
                            "category": r.category,
                            "paraphrase": r.paraphrase,
                            "fresh_score": r.fresh_score,
                            "paraphrased_score": r.paraphrased_score,
                            "delta": r.delta,
                            "tier": r.paraphrased_tier,
                        }
                        for r in exp.rows
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
            "Run the eval suite twice — once with the basic golden set "
            "(primes the cache) and once with paraphrased prompts. "
            "Measures whether the L2 semantic cache serves paraphrases "
            "AND preserves answer quality."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--base-url", default="http://localhost:8080")
    p.add_argument("--api-key", default=os.environ.get("PRONAOS_DEMO_API_KEY"))
    p.add_argument("--candidate-model", default="groq/llama-3.1-8b-instant")
    p.add_argument("--judge-model", default="groq/llama-3.3-70b-versatile")
    p.add_argument(
        "--basic", type=Path, default=Path("tests/eval/data/basic.yaml")
    )
    p.add_argument(
        "--paraphrased",
        type=Path,
        default=Path("tests/eval/data/basic_paraphrased.yaml"),
    )
    p.add_argument(
        "--epsilon",
        type=float,
        default=0.05,
        help="Max per-case Δscore tolerated before declaring a trade-off.",
    )
    p.add_argument("--output", type=Path, default=None)
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
                basic_path=args.basic,
                paraphrased_path=args.paraphrased,
                epsilon=args.epsilon,
                output_json=args.output,
            )
        )
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
