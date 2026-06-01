"""BFCL-style tool-use accuracy experiment (Claim #32, Phase 45).

The empirical question
----------------------
Pronaos already measures answer quality (Claim #10 multi-judge,
Claim #11 quality-aware routing). This script measures a *different*
dimension: **how well does each candidate model invoke the right
tool with the right arguments?**

The Berkeley Function-Calling Leaderboard (BFCL) is the standard
benchmark for this. This script runs a small, curated BFCL-style
golden set (12 cases across simple / selection / arguments /
relevance / parallel categories) through the gateway against
several candidate models, scores each response, and reports a
per-model accuracy table.

Method
------
1. Load ``tests/eval/data/tool_use_basic.yaml`` (12 cases).
2. For each ``--candidate`` model:
   a. For each case: POST a chat completion with the case's prompt
      + tool definitions to the gateway with that model.
   b. Score the response with ``core.tool_use_eval.score_case``.
3. Aggregate per-model: total passed, per-category breakdown,
   per-case failure reasons.
4. Print a sorted table. VERDICT line on whether the eval
   differentiates models (range ≥ 10% suggests informative
   signal).

Honesty
-------
- 12 cases is a starter set. The real BFCL has hundreds; tighter
  statistical bounds need a larger evaluation set, which is a
  worthy follow-up.
- Each case is scored exact-match on function name + AST-equivalent
  args. A model that returned the right tool with slightly different
  formatting (e.g. "Paris, France" vs "Paris") fails. This is the
  BFCL spec — sloppy tool-use is wrong tool-use.
- Cost-per-call is read from the gateway's ``pronaos.cost_hcents``
  response block, the authoritative source for per-call FinOps.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

import httpx

from pronaos.core.tool_use_eval import (
    ToolUseCase,
    ToolUseSummary,
    load_golden_set,
    score_case,
    summarize,
)

DEFAULT_CANDIDATES = [
    "groq/llama-3.1-8b-instant",
    "groq/llama-3.3-70b-versatile",
    "groq/meta-llama/llama-4-scout-17b-16e-instruct",
]


async def _one_call(
    *,
    client: httpx.AsyncClient,
    api_key: str,
    model: str,
    case: ToolUseCase,
) -> tuple[int, dict[str, Any]]:
    body = {
        "model": model,
        "messages": [{"role": "user", "content": case.prompt}],
        "tools": case.tools,
        "tool_choice": "auto",
        "temperature": 0.0,
        "max_tokens": 200,
    }
    resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json=body,
        timeout=60.0,
    )
    try:
        data: dict[str, Any] = resp.json()
    except ValueError:
        data = {"_raw": resp.text}
    return resp.status_code, data


def _format_summary(s: ToolUseSummary) -> str:
    lines = [
        f"  {s.model:50}  {s.passed}/{s.total}  ({s.accuracy * 100:5.1f}%)"
    ]
    if s.by_category:
        cat_strs = [
            f"{cat}={p}/{t}"
            for cat, (p, t) in sorted(s.by_category.items())
        ]
        lines.append(f"      by category: {', '.join(cat_strs)}")
    fails = [(cid, r) for cid, ok, r in s.per_case if not ok]
    if fails:
        lines.append("      failed cases:")
        for cid, reason in fails:
            lines.append(f"        - {cid:30}  reason={reason}")
    return "\n".join(lines)


async def _main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--gateway-url", default="http://127.0.0.1:8080")
    parser.add_argument("--api-key", required=True)
    parser.add_argument(
        "--golden-set",
        default="tests/eval/data/tool_use_basic.yaml",
    )
    parser.add_argument(
        "--candidates",
        default=",".join(DEFAULT_CANDIDATES),
        help="Comma-separated list of candidate model fqmns.",
    )
    parser.add_argument(
        "--min-spread",
        type=float,
        default=10.0,
        help=(
            "Minimum per-model accuracy spread (%) for the eval to "
            "count as informative. Below this, the verdict reports "
            "'underdiscriminating' (need a harder set)."
        ),
    )
    args = parser.parse_args()

    golden_path = Path(args.golden_set)
    if not golden_path.exists():
        print(f"ERROR: golden set not found: {golden_path}", file=sys.stderr)
        sys.exit(2)

    cases = load_golden_set(str(golden_path))
    candidates = [m.strip() for m in args.candidates.split(",") if m.strip()]

    print("=" * 72)
    print("Phase 45 — BFCL-style tool-use accuracy experiment")
    print("=" * 72)
    print(f"golden set:  {golden_path} ({len(cases)} cases)")
    print(f"candidates:  {len(candidates)} models")
    for m in candidates:
        print(f"             - {m}")
    print()

    summaries: list[ToolUseSummary] = []
    async with httpx.AsyncClient(base_url=args.gateway_url, timeout=60.0) as client:
        for model in candidates:
            print(f"running {model}...")
            scores = []
            for c in cases:
                status, body = await _one_call(
                    client=client, api_key=args.api_key, model=model, case=c
                )
                if status >= 400:
                    print(
                        f"  {c.case_id:30}  HTTP {status}: "
                        f"{str(body)[:120]} (treated as missing_call)"
                    )
                    from pronaos.core.tool_use_eval import ToolUseScore

                    scores.append(
                        ToolUseScore(
                            case_id=c.case_id,
                            passed=False,
                            reason=f"http_{status}",
                        )
                    )
                    continue
                scores.append(score_case(c, body))
            s = summarize(model, cases, scores)
            summaries.append(s)
            print(_format_summary(s))
            print()

    # ---- Verdict --------------------------------------------------------
    if not summaries:
        print("VERDICT: claim fails — no models scored.")
        sys.exit(1)

    sorted_summaries = sorted(summaries, key=lambda s: -s.accuracy)
    accuracies = [s.accuracy for s in sorted_summaries]
    spread = (max(accuracies) - min(accuracies)) * 100

    print("=" * 72)
    print("Final ranking (highest accuracy first)")
    print("=" * 72)
    for s in sorted_summaries:
        print(_format_summary(s))
    print()
    print(f"per-model accuracy spread:  {spread:.1f}%")
    print()

    if spread >= args.min_spread:
        print(
            f"VERDICT: claim holds — the BFCL-style eval differentiates "
            f"models on tool-use accuracy. Best: "
            f"{sorted_summaries[0].model} at {accuracies[0] * 100:.1f}%. "
            f"Worst: {sorted_summaries[-1].model} at {accuracies[-1] * 100:.1f}%. "
            f"Per-model spread = {spread:.1f}% (threshold {args.min_spread}%); "
            f"the gateway now has a per-model tool-use accuracy signal that "
            f"can feed routing decisions (extends Claim #11's quality-aware "
            f"routing into the tool-call dimension)."
        )
        sys.exit(0)

    print(
        f"VERDICT: eval is UNDERDISCRIMINATING — per-model accuracy "
        f"spread = {spread:.1f}% < threshold {args.min_spread}%. The "
        f"candidate models tied closely on this 12-case set. The eval "
        f"still ran; the result is informative as a baseline. Tighter "
        f"differentiation needs a larger / harder golden set or wider "
        f"candidate variety."
    )
    sys.exit(1)


if __name__ == "__main__":
    asyncio.run(_main())
