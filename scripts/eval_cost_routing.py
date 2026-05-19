"""Cost-aware routing experiment (Phase 21).

Question: how much cost does auto-routing save, and at what quality cost?

Method
------
For each prompt in the golden set:
  1. Send it twice through the gateway:
     - "manual" mode: pin to the team's expensive default
       (``groq/llama-3.3-70b-versatile``)
     - "auto" mode: send ``model="auto"`` — gateway picks the cheapest
       eligible model (Phase 21 scorer)
  2. Read the gateway-authoritative cost from ``response.pronaos.cost_hcents``
  3. Score both responses with the same LLM judge
  4. Aggregate: total cost manual vs auto, mean quality score, pass rate

The team's ``allowed_models`` should permit both the expensive and
cheap models (e.g. ``["groq/*"]``) and ``routing_strategy`` should be
``cheapest`` so auto-routing picks the lower-cost variant.

Why this matters
----------------
The headline FinOps claim: "auto-routing reduces cost by X% at Y%
quality." If quality drops too far the claim fails — that's the
falsifiable property the script tests.

Output
------
A single summary block with:
- Per-mode: total cost, mean score, scored cases, pass rate at >=0.7
- Delta: cost reduction %, quality delta in absolute score
- Verdict: claim holds / fails based on configurable thresholds
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from pronaos.eval.data import EvalCase, load_golden_set
from pronaos.eval.scorer import LLMJudgeScorer


# --------------------------------------------------------------------------- #
# Per-call result                                                             #
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class CallResult:
    case_id: str
    score: float
    cost_hcents: int
    routed_model: str
    error: str | None = None

    @property
    def is_scored(self) -> bool:
        return self.error is None


@dataclass(slots=True)
class ModeAgg:
    """Aggregate across all cases for one routing mode."""

    label: str
    results: list[CallResult] = field(default_factory=list)

    @property
    def total_cost_hcents(self) -> int:
        return sum(r.cost_hcents for r in self.results)

    @property
    def total_cost_dollars(self) -> float:
        return self.total_cost_hcents / 10_000.0  # hcents → dollars

    @property
    def mean_score(self) -> float:
        scored = [r.score for r in self.results if r.is_scored]
        return sum(scored) / len(scored) if scored else 0.0

    @property
    def scored_count(self) -> int:
        return sum(1 for r in self.results if r.is_scored)

    @property
    def pass_rate(self) -> float:
        scored = [r for r in self.results if r.is_scored]
        if not scored:
            return 0.0
        passing = sum(1 for r in scored if r.score >= 0.7)
        return passing / len(scored)


# --------------------------------------------------------------------------- #
# One call through the gateway                                                #
# --------------------------------------------------------------------------- #


async def _fire_case(
    client: httpx.AsyncClient,
    *,
    api_key: str,
    model: str,
    case: EvalCase,
    scorer: LLMJudgeScorer,
) -> CallResult:
    try:
        resp = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": case.prompt}],
                "temperature": 0.0,
                "max_tokens": 400,
            },
            timeout=60.0,
        )
    except Exception as e:  # noqa: BLE001
        return CallResult(
            case_id=case.id,
            score=0.0,
            cost_hcents=0,
            routed_model=model,
            error=f"network: {e}",
        )

    if resp.status_code != 200:
        return CallResult(
            case_id=case.id,
            score=0.0,
            cost_hcents=0,
            routed_model=model,
            error=f"http {resp.status_code}: {resp.text[:160]}",
        )

    body = resp.json()
    text = body["choices"][0]["message"]["content"]
    cost = int(body.get("pronaos", {}).get("cost_hcents", 0))
    # For auto requests the gateway surfaces the picked model in the header;
    # for explicit requests this header is absent — fall back to body.model.
    routed = resp.headers.get("X-Pronaos-Routed-Model") or body.get("model", model)

    try:
        result = await scorer.score(
            prompt=case.prompt, expected=case.expected, candidate=text
        )
        if result.judge_error:
            return CallResult(
                case_id=case.id,
                score=0.0,
                cost_hcents=cost,
                routed_model=routed,
                error=f"judge: {result.judge_error}",
            )
        return CallResult(
            case_id=case.id,
            score=result.score,
            cost_hcents=cost,
            routed_model=routed,
        )
    except Exception as e:  # noqa: BLE001
        return CallResult(
            case_id=case.id,
            score=0.0,
            cost_hcents=cost,
            routed_model=routed,
            error=f"score: {e}",
        )


# --------------------------------------------------------------------------- #
# Runner                                                                      #
# --------------------------------------------------------------------------- #


async def _run_mode(
    *,
    client: httpx.AsyncClient,
    api_key: str,
    model: str,
    cases: list[EvalCase],
    scorer: LLMJudgeScorer,
    label: str,
) -> ModeAgg:
    """Run one routing mode against the full case set sequentially.

    Sequential not concurrent — keeps the per-case console output legible
    and avoids tripping per-key RPS limits at low burst caps.
    """
    print(f"\n[{label}] running {len(cases)} cases with model={model!r}")
    agg = ModeAgg(label=label)
    for i, case in enumerate(cases, 1):
        result = await _fire_case(
            client, api_key=api_key, model=model, case=case, scorer=scorer
        )
        marker = "ok" if result.is_scored else "ERR"
        print(
            f"  [{i:>2}/{len(cases)}] {result.case_id:<25} "
            f"score={result.score:.2f} cost={result.cost_hcents}hcents "
            f"model={result.routed_model} {marker}"
        )
        if result.error:
            print(f"      ! {result.error}")
        agg.results.append(result)
    return agg


def _print_summary(manual: ModeAgg, auto: ModeAgg) -> int:
    """Render the headline summary block. Returns exit code (0 = claim holds)."""
    print("\n" + "=" * 72)
    print("Phase 21 — Cost-aware routing experiment")
    print("=" * 72)
    print(f"{'mode':<10}  {'scored':>7}  {'pass-rate':>10}  "
          f"{'mean':>6}  {'total cost':>14}")
    for agg in (manual, auto):
        print(
            f"{agg.label:<10}  {agg.scored_count:>7}  "
            f"{agg.pass_rate:>9.1%}  {agg.mean_score:>6.3f}  "
            f"${agg.total_cost_dollars:>10.6f} "
            f"({agg.total_cost_hcents}hcents)"
        )
    if manual.total_cost_hcents <= 0:
        cost_delta_pct = 0.0
    else:
        cost_delta_pct = (
            (manual.total_cost_hcents - auto.total_cost_hcents)
            / manual.total_cost_hcents
            * 100.0
        )
    quality_delta = auto.mean_score - manual.mean_score
    print()
    print(f"cost reduction: {cost_delta_pct:+.1f}%")
    print(f"quality delta:  {quality_delta:+.3f} (auto - manual)")

    # Verdict thresholds — claim holds if cost dropped meaningfully without
    # quality collapsing. These thresholds are chosen so a "no-op" no-savings
    # routing or a quality-crashing routing both fail the verdict.
    if cost_delta_pct >= 30.0 and quality_delta >= -0.10:
        print(
            "VERDICT: claim holds — cost-aware routing saves money "
            "at acceptable quality."
        )
        return 0
    print(
        "VERDICT: claim does not hold — either the savings are marginal "
        "or quality dropped too far."
    )
    return 1


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #


async def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--gateway-url",
        default="http://127.0.0.1:8123",
        help="Pronaos gateway base URL.",
    )
    parser.add_argument(
        "--api-key",
        required=True,
        help="Pronaos API key with chat:write scope.",
    )
    parser.add_argument(
        "--golden-set",
        default="tests/eval/data/basic.yaml",
        help="Path to the golden-set YAML file.",
    )
    parser.add_argument(
        "--manual-model",
        default="groq/llama-3.3-70b-versatile",
        help=(
            "Concrete model to use for the 'manual' baseline run. "
            "Choose the team's typical/expensive default."
        ),
    )
    parser.add_argument(
        "--judge-model",
        default="groq/llama-3.3-70b-versatile",
        help="Model the LLM judge uses to score responses.",
    )
    parser.add_argument(
        "--judge-api-key",
        default=None,
        help=(
            "Optional separate API key for the judge. If omitted, the "
            "judge calls the gateway directly via the same --api-key."
        ),
    )
    args = parser.parse_args()

    cases = list(load_golden_set(Path(args.golden_set)).cases)
    if not cases:
        print("no cases in golden set — refusing to run.", file=sys.stderr)
        return 2

    async with httpx.AsyncClient(base_url=args.gateway_url) as client:
        scorer = LLMJudgeScorer(
            base_url=args.gateway_url,
            api_key=args.judge_api_key or args.api_key,
            judge_model=args.judge_model,
        )
        # Manual baseline — pin the expensive model.
        manual = await _run_mode(
            client=client,
            api_key=args.api_key,
            model=args.manual_model,
            cases=cases,
            scorer=scorer,
            label="manual",
        )
        # Auto-routing — let the gateway pick the cheapest model.
        auto = await _run_mode(
            client=client,
            api_key=args.api_key,
            model="auto",
            cases=cases,
            scorer=scorer,
            label="auto",
        )
        return _print_summary(manual, auto)


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
