"""Multi-model cost-quality benchmark.

Question: how much quality am I trading away by picking a cheaper
candidate model? Or, equivalently: how much am I overpaying by
defaulting to the biggest model when a smaller one would do?

Method
------
For each candidate model in ``--models``:

1. Run the same golden set through the gateway
2. For each case, read ``response.pronaos.cost_hcents`` to capture the
   gateway's authoritative cost for that call
3. Score every response with the SAME judge model (constant across runs)
4. Aggregate per model: mean score, total cost, pass-rate at threshold,
   cost-per-correct-answer

Why a fixed judge: comparing candidates requires the same scoring
rubric and the same grader. If you used a different judge per
candidate, the experiment is testing the judges, not the candidates.

Self-judging caveat
-------------------
Avoid running with ``--judge-model`` set to a model that's also in
``--models``. A judge scoring its own outputs is biased toward
agreement (it agrees with its own writing style). The script warns
but doesn't refuse — sometimes you genuinely want a "ceiling" line.

Cache control
-------------
Each candidate model has its own cache namespace (cache keys include
the model id), so cross-model interference is impossible. WITHIN a
single run, each prompt is unique → exactly one cache miss per case.
The script doesn't clear caches between models — the experiment
measures the gateway's natural behaviour, which includes its caches.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from pronaos.eval.data import EvalCase, load_golden_set
from pronaos.eval.scorer import LLMJudgeScorer, ScoreResult

# --------------------------------------------------------------------------- #
# Per-case result                                                             #
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class CaseResult:
    case_id: str
    category: str
    score: float
    cost_hcents: int
    prompt_tokens: int
    completion_tokens: int
    error: str | None = None

    @property
    def is_scored(self) -> bool:
        return self.error is None


async def _fire_case(
    client: httpx.AsyncClient,
    *,
    api_key: str,
    candidate_model: str,
    case: EvalCase,
    scorer: LLMJudgeScorer,
) -> CaseResult:
    """Send one prompt + score response. Returns CaseResult with cost
    parsed from the gateway's authoritative ``pronaos`` field."""
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
            score=0.0,
            cost_hcents=0,
            prompt_tokens=0,
            completion_tokens=0,
            error=f"network: {e}",
        )

    if resp.status_code != 200:
        return CaseResult(
            case_id=case.id,
            category=case.category,
            score=0.0,
            cost_hcents=0,
            prompt_tokens=0,
            completion_tokens=0,
            error=f"http {resp.status_code}: {resp.text[:160]}",
        )

    body = resp.json()
    text = body["choices"][0]["message"]["content"]
    cost_hcents = int(body.get("pronaos", {}).get("cost_hcents", 0))
    usage = body.get("usage", {})
    prompt_tokens = int(usage.get("prompt_tokens", 0))
    completion_tokens = int(usage.get("completion_tokens", 0))

    verdict: ScoreResult = await scorer.score(
        prompt=case.prompt, expected=case.expected, candidate=text
    )

    return CaseResult(
        case_id=case.id,
        category=case.category,
        score=verdict.score,
        cost_hcents=cost_hcents,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        error=verdict.judge_error,
    )


# --------------------------------------------------------------------------- #
# Per-model aggregate                                                         #
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class ModelSummary:
    model: str
    rows: list[CaseResult] = field(default_factory=list)

    @property
    def scored(self) -> list[CaseResult]:
        return [r for r in self.rows if r.is_scored]

    @property
    def mean_score(self) -> float:
        s = self.scored
        return sum(r.score for r in s) / len(s) if s else 0.0

    @property
    def total_cost_hcents(self) -> int:
        return sum(r.cost_hcents for r in self.scored)

    @property
    def total_tokens(self) -> int:
        return sum(r.prompt_tokens + r.completion_tokens for r in self.scored)

    @property
    def cost_per_call_hcents(self) -> float:
        s = self.scored
        return self.total_cost_hcents / len(s) if s else 0.0

    def pass_count(self, threshold: float) -> int:
        return sum(1 for r in self.scored if r.score >= threshold)

    def cost_per_correct_hcents(self, threshold: float) -> float:
        n_pass = self.pass_count(threshold)
        return self.total_cost_hcents / n_pass if n_pass else float("inf")


# --------------------------------------------------------------------------- #
# Experiment                                                                  #
# --------------------------------------------------------------------------- #


async def run_benchmark(
    *,
    base_url: str,
    api_key: str,
    candidate_models: list[str],
    judge_model: str,
    golden_set_path: Path,
    pass_threshold: float,
    output_json: Path | None,
) -> int:
    golden = load_golden_set(golden_set_path)
    if judge_model in candidate_models:
        print(
            f"⚠ note: judge_model={judge_model!r} also appears in candidate_models; "
            f"that candidate's score will be biased toward 1.0 (self-grading).",
            file=sys.stderr,
        )

    print("Pronaos multi-model cost-quality benchmark")
    print("=" * 78)
    print(f"golden set:   {golden.name} ({len(golden)} cases)")
    print(f"judge:        {judge_model}")
    print(f"candidates:   {len(candidate_models)}")
    for m in candidate_models:
        print(f"              - {m}")
    print(f"pass ≥:       {pass_threshold:.2f}")
    print()

    scorer = LLMJudgeScorer(
        base_url=base_url, api_key=api_key, judge_model=judge_model
    )

    summaries: list[ModelSummary] = []
    async with httpx.AsyncClient(base_url=base_url) as client:
        for model in candidate_models:
            print(f"=== {model} ===")
            t0 = time.monotonic()
            summary = ModelSummary(model=model)
            for case in golden.cases:
                r = await _fire_case(
                    client,
                    api_key=api_key,
                    candidate_model=model,
                    case=case,
                    scorer=scorer,
                )
                summary.rows.append(r)
            dt = time.monotonic() - t0
            errors = [r for r in summary.rows if not r.is_scored]
            print(
                f"  mean: {summary.mean_score:.3f}  pass: "
                f"{summary.pass_count(pass_threshold)}/{len(golden)}  "
                f"cost: {summary.total_cost_hcents}hc "
                f"(${summary.total_cost_hcents/10_000:.4f})  "
                f"wall: {dt:.1f}s"
            )
            if errors:
                print(f"  ⚠ errors: {len(errors)} (excluded from aggregates)")
            print()
            summaries.append(summary)

    # ---- Final table -------------------------------------------------------
    print("=" * 78)
    print("RESULTS")
    print()
    _print_table(summaries, pass_threshold)

    # ---- Markdown for README ----------------------------------------------
    print()
    print("=" * 78)
    print("MARKDOWN (paste into README):")
    print()
    print(_markdown_table(summaries, pass_threshold))

    if output_json is not None:
        output_json.parent.mkdir(parents=True, exist_ok=True)
        output_json.write_text(
            json.dumps(
                {
                    "golden_set": golden.name,
                    "judge_model": judge_model,
                    "pass_threshold": pass_threshold,
                    "results": [
                        {
                            "model": s.model,
                            "mean_score": s.mean_score,
                            "pass_count": s.pass_count(pass_threshold),
                            "total_cases": len(s.rows),
                            "total_cost_hcents": s.total_cost_hcents,
                            "cost_per_call_hcents": s.cost_per_call_hcents,
                            "cost_per_correct_hcents": _safe_cpc(
                                s, pass_threshold
                            ),
                            "total_tokens": s.total_tokens,
                            "rows": [
                                {
                                    "case_id": r.case_id,
                                    "category": r.category,
                                    "score": r.score,
                                    "cost_hcents": r.cost_hcents,
                                    "prompt_tokens": r.prompt_tokens,
                                    "completion_tokens": r.completion_tokens,
                                    "error": r.error,
                                }
                                for r in s.rows
                            ],
                        }
                        for s in summaries
                    ],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        print()
        print(f"saved: {output_json}")

    return 0


def _safe_cpc(s: ModelSummary, threshold: float) -> float | None:
    """Cost-per-correct returns inf when zero cases pass — represent as
    None in the JSON instead so consumers can decide how to display."""
    cpc = s.cost_per_correct_hcents(threshold)
    return None if cpc == float("inf") else cpc


def _print_table(summaries: list[ModelSummary], threshold: float) -> None:
    print(
        f"  {'model':<38} {'mean':>5} {'pass':>5} {'$/call':>10} {'$/correct':>11}"
    )
    print("  " + "-" * 76)
    for s in summaries:
        cpc = s.cost_per_correct_hcents(threshold)
        cpc_str = "∞" if cpc == float("inf") else f"${cpc/10_000:.6f}"
        print(
            f"  {s.model:<38} {s.mean_score:>5.2f} "
            f"{s.pass_count(threshold)}/{len(s.rows):<3} "
            f"${s.cost_per_call_hcents/10_000:>8.6f}  {cpc_str:>11}"
        )


def _markdown_table(summaries: list[ModelSummary], threshold: float) -> str:
    """Output the same table in markdown for direct README paste."""
    lines = [
        f"| Model | Mean score | Pass rate (≥{threshold:.2f}) | $ / call | $ / correct |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for s in summaries:
        cpc = s.cost_per_correct_hcents(threshold)
        cpc_str = "∞" if cpc == float("inf") else f"${cpc/10_000:.6f}"
        lines.append(
            f"| `{s.model}` "
            f"| {s.mean_score:.3f} "
            f"| {s.pass_count(threshold)}/{len(s.rows)} "
            f"| ${s.cost_per_call_hcents/10_000:.6f} "
            f"| {cpc_str} |"
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Run the eval suite against multiple candidate models and report "
            "cost-per-correct-answer."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--base-url", default="http://localhost:8080")
    p.add_argument("--api-key", default=os.environ.get("PRONAOS_DEMO_API_KEY"))
    p.add_argument(
        "--models",
        default="groq/llama-3.1-8b-instant,groq/mixtral-8x7b-32768,groq/qwen-qwq-32b",
        help="Comma-separated candidate model ids.",
    )
    p.add_argument("--judge-model", default="groq/llama-3.3-70b-versatile")
    p.add_argument(
        "--golden-set",
        type=Path,
        default=Path("tests/eval/data/basic.yaml"),
    )
    p.add_argument("--threshold", type=float, default=0.7)
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
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    if not models:
        print("error: --models cannot be empty", file=sys.stderr)
        return 2
    try:
        return asyncio.run(
            run_benchmark(
                base_url=args.base_url,
                api_key=args.api_key,
                candidate_models=models,
                judge_model=args.judge_model,
                golden_set_path=args.golden_set,
                pass_threshold=args.threshold,
                output_json=args.output,
            )
        )
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
