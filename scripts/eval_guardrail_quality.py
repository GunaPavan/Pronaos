"""Guardrail-quality experiment.

Question: does PII redaction degrade answer quality?

Every team running an LLM gateway wants to redact PII before it
reaches a third-party provider. But almost no team measures whether
the redaction tokens (``[REDACTED-EMAIL]`` etc.) confuse the model
enough to degrade its answers. This experiment measures exactly that.

Method
------
For each case in the golden set we have a matched pair:

1. ``basic.yaml`` — clean prompt, no PII. Provider sees it unmodified.
2. ``basic_with_pii.yaml`` — same intent + same rubric, but the prompt
   contains incidental PII (e.g. "What is the capital of France? My
   email is alice@example.com if you want to follow up."). The
   gateway's ingress guardrail redacts the PII. Provider sees the
   ``[REDACTED-EMAIL]`` form.

The judge model scores both responses against the SAME rubric. If
redaction is neutral, Δscore ≈ 0. If redaction confuses the model,
Δscore > 0 — a real cost to quantify.

Three outcomes are publishable
------------------------------

1. **Δ ≈ 0**: ✅ "Redacting incidental PII doesn't degrade answer
   quality on the rubric set." The strongest claim — teams want this
   to be true and rarely measure it.
2. **Δ > 0 on a few cases**: trade-off. "Redaction costs X% on
   questions that hinge on the redacted token." Actionable.
3. **Δ < 0**: rare. "Redaction *helps* — the PII was a distractor."

Cache control
-------------
We clear caches between runs. Without that, the second run's prompts
would either L1-miss (different text) or L2-hit (if similarity > 0.95)
and confound the experiment. Clear → both runs hit the provider.

(Note: this script clears Qdrant POINTS not the collection — see
eval_paraphrase_cache_quality.py docs for the reason. Cache must have
been previously initialised by the gateway at startup.)
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
# Cache clearing — same approach as eval_paraphrase_cache_quality.py          #
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


async def _clear_qdrant_points(qdrant_url: str = "http://localhost:6333") -> bool:
    """Empty the collection's points, not the collection itself."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                f"{qdrant_url}/collections/pronaos_semantic_cache/points/delete",
                json={"filter": {"must": []}},
            )
            return resp.status_code in (200, 202, 404)
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# Single-case runner — captures guardrail header + score                      #
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class CaseResult:
    case_id: str
    category: str
    prompt: str
    response: str
    score: float
    justification: str
    guardrail_header: str = ""
    error: str | None = None

    @property
    def redacted_rules(self) -> list[str]:
        """Parse 'redacted:rule1,rule2' header into the rule list."""
        h = self.guardrail_header.lower()
        if not h.startswith("redacted:"):
            return []
        return [r.strip() for r in h.removeprefix("redacted:").split(",") if r.strip()]


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
            prompt=case.prompt,
            response="",
            score=0.0,
            justification="",
            error=f"network: {e}",
        )
    if resp.status_code != 200:
        return CaseResult(
            case_id=case.id,
            category=case.category,
            prompt=case.prompt,
            response="",
            score=0.0,
            justification="",
            error=f"http {resp.status_code}: {resp.text[:160]}",
        )
    text = resp.json()["choices"][0]["message"]["content"]
    header = resp.headers.get("x-pronaos-guardrails", "")
    verdict: ScoreResult = await scorer.score(
        prompt=case.prompt, expected=case.expected, candidate=text
    )
    return CaseResult(
        case_id=case.id,
        category=case.category,
        prompt=case.prompt,
        response=text,
        score=verdict.score,
        justification=verdict.justification,
        guardrail_header=header,
        error=verdict.judge_error,
    )


# --------------------------------------------------------------------------- #
# Experiment                                                                  #
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class CompareRow:
    case_id: str
    category: str
    clean_score: float
    redacted_score: float
    redacted_rules: list[str] = field(default_factory=list)

    @property
    def delta(self) -> float:
        return self.redacted_score - self.clean_score


async def run_experiment(
    *,
    base_url: str,
    api_key: str,
    candidate_model: str,
    judge_model: str,
    basic_path: Path,
    pii_path: Path,
    epsilon: float,
    output_json: Path | None,
) -> int:
    clean = load_golden_set(basic_path)
    redacted = load_golden_set(pii_path)
    clean_ids = {c.id for c in clean.cases}
    redacted_ids = {c.id for c in redacted.cases}
    if clean_ids != redacted_ids:
        print(
            f"error: case-id mismatch. "
            f"clean-only={clean_ids - redacted_ids}, "
            f"pii-only={redacted_ids - clean_ids}",
            file=sys.stderr,
        )
        return 2

    print("Pronaos guardrail-quality experiment")
    print("=" * 70)
    print(f"clean set:     {clean.name} ({len(clean)} cases)")
    print(f"redacted set:  {redacted.name} ({len(redacted)} cases)")
    print(f"candidate:     {candidate_model}")
    print(f"judge:         {judge_model}")
    print(f"epsilon:       {epsilon}")
    print()
    print("Method: send each prompt pair. The clean prompt goes to the")
    print("provider unmodified; the PII-augmented prompt goes through")
    print("the gateway's ingress guardrail and reaches the provider with")
    print("[REDACTED-*] tokens in place of PII. Same rubric grades both.")
    print()

    scorer = LLMJudgeScorer(
        base_url=base_url, api_key=api_key, judge_model=judge_model
    )

    # --- run 1: clean baseline -----------------------------------------------
    print(f"[1/2] clearing caches + running {len(clean)} clean prompts...")
    redis_ok = _clear_redis()
    qdrant_ok = await _clear_qdrant_points()
    print(f"      redis cleared: {redis_ok}  qdrant points cleared: {qdrant_ok}")
    t0 = time.monotonic()
    clean_results: dict[str, CaseResult] = {}
    async with httpx.AsyncClient(base_url=base_url) as client:
        for case in clean.cases:
            clean_results[case.id] = await _fire_case(
                client,
                api_key=api_key,
                candidate_model=candidate_model,
                case=case,
                scorer=scorer,
            )
    clean_dt = time.monotonic() - t0
    clean_scored = sum(1 for r in clean_results.values() if r.error is None)
    clean_mean = (
        sum(r.score for r in clean_results.values() if r.error is None) / clean_scored
        if clean_scored
        else 0.0
    )
    print(f"      scored: {clean_scored}/{len(clean)}  mean: {clean_mean:.3f}  wall: {clean_dt:.1f}s")
    print()

    # --- run 2: PII-injected, guardrail-redacted -----------------------------
    print(f"[2/2] clearing caches + running {len(redacted)} PII-injected prompts...")
    redis_ok = _clear_redis()
    qdrant_ok = await _clear_qdrant_points()
    print(f"      redis cleared: {redis_ok}  qdrant points cleared: {qdrant_ok}")
    t1 = time.monotonic()
    redacted_results: dict[str, CaseResult] = {}
    async with httpx.AsyncClient(base_url=base_url) as client:
        for case in redacted.cases:
            redacted_results[case.id] = await _fire_case(
                client,
                api_key=api_key,
                candidate_model=candidate_model,
                case=case,
                scorer=scorer,
            )
    redacted_dt = time.monotonic() - t1
    redacted_scored = sum(1 for r in redacted_results.values() if r.error is None)
    redacted_mean = (
        sum(r.score for r in redacted_results.values() if r.error is None)
        / redacted_scored
        if redacted_scored
        else 0.0
    )
    print(f"      scored: {redacted_scored}/{len(redacted)}  mean: {redacted_mean:.3f}  wall: {redacted_dt:.1f}s")
    print()

    # --- compare -------------------------------------------------------------
    rows: list[CompareRow] = []
    redactions_fired = 0
    for cid in sorted(clean_ids):
        c = clean_results[cid]
        r = redacted_results[cid]
        if c.error or r.error:
            continue
        rules = r.redacted_rules
        if rules:
            redactions_fired += 1
        rows.append(
            CompareRow(
                case_id=cid,
                category=c.category,
                clean_score=c.score,
                redacted_score=r.score,
                redacted_rules=rules,
            )
        )

    print("=" * 70)
    print("PER-CASE")
    print(
        f"  {'case':<22} {'clean':>5} {'redact':>6} {'Δ':>6}  redacted rules"
    )
    for row in rows:
        flag = " ⚠" if abs(row.delta) > epsilon else ""
        rules_str = ",".join(row.redacted_rules) if row.redacted_rules else "(none)"
        print(
            f"  {row.case_id:<22} {row.clean_score:>5.2f} {row.redacted_score:>6.2f} "
            f"{row.delta:>+6.2f}  {rules_str}{flag}"
        )

    n = len(rows)
    deltas = [r.delta for r in rows]
    mean_delta = sum(deltas) / n if n else 0.0
    max_abs = max(abs(d) for d in deltas) if deltas else 0.0
    over_eps = [r for r in rows if abs(r.delta) > epsilon]
    clean_mean_compared = sum(r.clean_score for r in rows) / n if n else 0.0
    redacted_mean_compared = sum(r.redacted_score for r in rows) / n if n else 0.0

    print()
    print("=" * 70)
    print("AGGREGATE")
    print(f"  cases compared:        {n}")
    print(f"  cases with redactions: {redactions_fired}")
    print(f"  clean mean:            {clean_mean_compared:.3f}")
    print(f"  redacted mean:         {redacted_mean_compared:.3f}")
    print(f"  mean Δ:                {mean_delta:+.4f}")
    print(f"  max |Δ|:               {max_abs:.4f}")
    print(f"  cases over ε:          {len(over_eps)} / {n}")
    print()

    # --- verdict -------------------------------------------------------------
    print("=" * 70)
    if redactions_fired == 0:
        print(
            "ℹ️  NO REDACTIONS FIRED. The basic_with_pii.yaml prompts didn't\n"
            "   match any guardrail rule. Check that PRONAOS_GUARDRAILS_ENABLED\n"
            "   is true and that the prompts contain detectable PII patterns."
        )
        verdict = "no_redactions"
        exit_code = 2
    elif max_abs <= epsilon:
        print(
            f"✅ CLAIM HOLDS: PII redaction preserves answer quality.\n"
            f"   Across {n} matched cases, redaction fired on {redactions_fired},\n"
            f"   and the redacted-response mean ({redacted_mean_compared:.3f})\n"
            f"   matches the clean mean ({clean_mean_compared:.3f}) within\n"
            f"   ε={epsilon} on every case.\n"
            f"   Headline: max per-case Δ = {max_abs:.4f}."
        )
        verdict = "preserves_quality"
        exit_code = 0
    else:
        print(
            f"⚠ QUALITY TRADE-OFF DETECTED.\n"
            f"   {len(over_eps)}/{n} cases shifted by more than ε={epsilon}\n"
            f"   under redaction. Max |Δ| = {max_abs:.4f}.\n"
            f"   This is real FinOps data: redaction is costing answer\n"
            f"   quality on some prompts. Consider per-tenant policy."
        )
        verdict = "quality_tradeoff"
        exit_code = 1

    if output_json is not None:
        output_json.parent.mkdir(parents=True, exist_ok=True)
        output_json.write_text(
            json.dumps(
                {
                    "verdict": verdict,
                    "candidate_model": candidate_model,
                    "judge_model": judge_model,
                    "epsilon": epsilon,
                    "cases": n,
                    "redactions_fired": redactions_fired,
                    "clean_mean": clean_mean_compared,
                    "redacted_mean": redacted_mean_compared,
                    "mean_delta": mean_delta,
                    "max_abs_delta": max_abs,
                    "rows": [
                        {
                            "case_id": r.case_id,
                            "category": r.category,
                            "clean_score": r.clean_score,
                            "redacted_score": r.redacted_score,
                            "delta": r.delta,
                            "redacted_rules": r.redacted_rules,
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
            "Compare answer quality between clean prompts and the same "
            "prompts with PII added (gateway redacts before sending to provider)."
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
        "--pii",
        type=Path,
        default=Path("tests/eval/data/basic_with_pii.yaml"),
    )
    p.add_argument("--epsilon", type=float, default=0.05)
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
                pii_path=args.pii,
                epsilon=args.epsilon,
                output_json=args.output,
            )
        )
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
