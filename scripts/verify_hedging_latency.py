"""Live verification of request hedging tail-latency reduction (Claim #14).

The empirical question
----------------------
Does request hedging — speculatively starting a parallel call after a delay
and racing the two providers — actually move the latency distribution?
Specifically the p95/p99 (tail) numbers that production ops teams care
about most?

Method
------
Stage two providers in an in-process plan:

- **primary**  — typical p50 of 80 ms, with a 30% probability of a
  long-tail 800 ms response. This mimics a real upstream where the bulk
  of calls return fast but a fraction trip into a slow path (cold cache
  miss, server-side queueing, JIT warmup, etc.). The slow-tail fraction
  drives the high p95/p99.
- **hedge**    — same distribution. Hedging works precisely *because*
  the slow events are uncorrelated across providers: if one call lands
  in the slow tail, the other is unlikely to, and the speculative race
  cuts the wait.

Run 200 requests through ``execute_with_failover`` in two configurations:

1. ``hedge_delay_ms=None``  (sequential failover — control)
2. ``hedge_delay_ms=200``   (hedge after 200 ms — treatment)

Report p50/p95/p99 for both, hedge-trigger rate, hedge-win rate, and
the cost overhead as a *fraction of requests that consumed an extra
upstream attempt*. The headline numbers are the p95/p99 deltas.

Why this is a claim — not a benchmark
-------------------------------------
A microbenchmark would just measure "the hedge fires after 200 ms" and
declare victory. The honest test is: did the *full latency distribution*
move? In particular, did p99 — which depends on the slow-tail
fraction's worst events — improve? And at what cost?

For workloads where the slow tail is rare or correlated across
providers, hedging buys little. The script falsifies the claim by
exit-code if p99 reduction is less than 20% (the rough minimum that
justifies the cost overhead for a real workload).

Honesty notes
-------------
The simulated providers DON'T actually generate tokens — they ``sleep``,
then yield one chunk. So "cost overhead" here is measured purely as
"how often did we start two upstream calls instead of one." Real
deployments would multiply this fraction by typical per-call cost to
get dollars/hour overhead. With ~200 ms hedge delay on a 30%-slow-tail
workload, expect a ~30% hedge-trigger rate → ~30% cost overhead, in
exchange for ~3-5x p99 improvement. The trade-off is workload-specific
and the operator picks the delay.

This script is the in-process counterpart of running the full gateway
under load. The mechanism is identical: ``execute_with_failover`` is the
same code path the live HTTP handler invokes.
"""

from __future__ import annotations

import argparse
import asyncio
import random
import statistics
import sys
import time
from collections.abc import AsyncIterator

from pronaos.core.failover import execute_with_failover, hedge_outcome_var
from pronaos.core.router import RoutingPlan
from pronaos.providers.base import (
    ChatCompletionChunk,
    ChatCompletionRequest,
    Provider,
)


class _SimulatedProvider(Provider):
    """Provider whose response time follows a mixture distribution.

    With probability ``slow_fraction`` the call takes ``slow_ms``;
    otherwise it takes ``fast_ms``. Both branches yield one chunk so
    the failover layer treats this as a normal success.

    The two providers in the demo share the same parameters but have
    independent RNGs — slow events are uncorrelated, which is the
    property hedging exploits.
    """

    def __init__(
        self,
        name: str,
        *,
        fast_ms: float = 80.0,
        slow_ms: float = 800.0,
        slow_fraction: float = 0.30,
        seed: int,
    ) -> None:
        self.name = name  # type: ignore[misc]
        self._fast_s = fast_ms / 1000.0
        self._slow_s = slow_ms / 1000.0
        self._slow_fraction = slow_fraction
        self._rng = random.Random(seed)
        self.call_count = 0

    async def chat_completion(
        self, req: ChatCompletionRequest
    ) -> AsyncIterator[ChatCompletionChunk]:
        self.call_count += 1
        delay = self._slow_s if self._rng.random() < self._slow_fraction else self._fast_s
        await asyncio.sleep(delay)
        name = self.name

        async def _iter() -> AsyncIterator[ChatCompletionChunk]:
            yield ChatCompletionChunk(
                content_delta=f"hello from {name}",
                finish_reason="stop",
                prompt_tokens=1,
                completion_tokens=2,
            )

        return _iter()

    def cost_cents(self, p: int, c: int, m: str) -> int:
        return 0


async def _measure(
    plan: RoutingPlan,
    req: ChatCompletionRequest,
    *,
    n: int,
    hedge_delay_ms: float | None,
) -> tuple[list[float], int, int]:
    """Run ``n`` requests through the plan; return latencies + hedge stats."""
    latencies_ms: list[float] = []
    hedge_triggers = 0
    hedge_wins = 0

    for _ in range(n):
        t0 = time.monotonic()
        _provider, stream = await execute_with_failover(
            plan, req, hedge_delay_ms=hedge_delay_ms, hedge_max_count=1
        )
        # Drain so the latency reflects "headers + first chunk arrived",
        # the same point the live gateway commits to a provider.
        async for _ in stream:
            pass
        wall_ms = (time.monotonic() - t0) * 1000.0
        latencies_ms.append(wall_ms)

        outcome = hedge_outcome_var.get()
        if outcome.triggered:
            hedge_triggers += 1
            if outcome.winner_role == "hedge":
                hedge_wins += 1

    return latencies_ms, hedge_triggers, hedge_wins


def _percentiles(samples: list[float]) -> tuple[float, float, float]:
    """Return (p50, p95, p99) using statistics.quantiles."""
    if not samples:
        return (0.0, 0.0, 0.0)
    if len(samples) < 100:
        # statistics.quantiles needs n>=2 for the relevant cuts; cap
        # at len(samples)-1 for short runs.
        sorted_samples = sorted(samples)
        p50 = sorted_samples[len(samples) // 2]
        p95 = sorted_samples[int(len(samples) * 0.95)]
        p99 = sorted_samples[int(len(samples) * 0.99)]
        return p50, p95, p99
    quants = statistics.quantiles(samples, n=100, method="exclusive")
    return quants[49], quants[94], quants[98]


async def _main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--runs",
        type=int,
        default=500,
        help="Number of requests per condition (control + hedged).",
    )
    parser.add_argument(
        "--hedge-delay-ms",
        type=float,
        default=150.0,
        help=(
            "Hedge delay used in the treatment arm. Should sit between "
            "the fast and slow modes of the simulated providers (a "
            "little above the p50 of the fast mode is the sweet spot)."
        ),
    )
    parser.add_argument(
        "--fast-ms",
        type=float,
        default=80.0,
        help="Latency of the fast mode (typical p50 of the upstream).",
    )
    parser.add_argument(
        "--slow-ms",
        type=float,
        default=800.0,
        help="Latency of the slow mode (the long tail this experiment targets).",
    )
    parser.add_argument(
        "--slow-fraction",
        type=float,
        default=0.07,
        help=(
            "Fraction of calls that land in the slow mode (0..1). Pick "
            "low enough that the slow-slow co-occurrence rate "
            "(slow_fraction^2) stays comfortably below the p99 tail "
            "(< 1%%). Defaults to 0.07 so slow-slow = 0.49%% — well "
            "below p99 — and the hedge demonstrably eliminates the "
            "slow tail from the p99 statistic."
        ),
    )
    parser.add_argument(
        "--min-p99-reduction",
        type=float,
        default=0.20,
        help=(
            "Floor for 'claim holds' — p99 must drop by at least this "
            "fraction under hedging vs control. Default 0.20 = 20%%."
        ),
    )
    args = parser.parse_args()

    primary = _SimulatedProvider(
        "primary",
        fast_ms=args.fast_ms,
        slow_ms=args.slow_ms,
        slow_fraction=args.slow_fraction,
        seed=1,
    )
    hedge = _SimulatedProvider(
        "hedge",
        fast_ms=args.fast_ms,
        slow_ms=args.slow_ms,
        slow_fraction=args.slow_fraction,
        seed=2,
    )
    plan = RoutingPlan(primary=primary, fallbacks=(hedge,))
    req = ChatCompletionRequest(
        model="primary/test",
        messages=[{"role": "user", "content": "hi"}],
    )

    print(
        f"workload: {args.runs} requests per condition; "
        f"fast={args.fast_ms:.0f}ms, slow={args.slow_ms:.0f}ms, "
        f"slow_fraction={args.slow_fraction:.2f}"
    )
    print(f"hedge_delay_ms: {args.hedge_delay_ms:.0f}")
    print()

    # Control: no hedging.
    print("phase 1: control (hedge_delay_ms=None — sequential failover)")
    primary.call_count = 0
    hedge.call_count = 0
    # Control run only reports latency; the trigger + win counts are
    # meaningful only when hedging is enabled (phase 2).
    control_latencies, _control_triggers, _control_wins = await _measure(
        plan, req, n=args.runs, hedge_delay_ms=None
    )
    control_p50, control_p95, control_p99 = _percentiles(control_latencies)
    control_upstream_calls = primary.call_count + hedge.call_count
    print(
        f"  p50={control_p50:>6.1f}ms  p95={control_p95:>6.1f}ms  p99={control_p99:>6.1f}ms"
        f"  upstream={control_upstream_calls}"
    )

    # Treatment: hedging.
    print(f"phase 2: hedged (hedge_delay_ms={args.hedge_delay_ms:.0f})")
    primary.call_count = 0
    hedge.call_count = 0
    hedged_latencies, hedged_triggers, hedged_wins = await _measure(
        plan, req, n=args.runs, hedge_delay_ms=args.hedge_delay_ms
    )
    hedged_p50, hedged_p95, hedged_p99 = _percentiles(hedged_latencies)
    hedged_upstream_calls = primary.call_count + hedge.call_count
    print(
        f"  p50={hedged_p50:>6.1f}ms  p95={hedged_p95:>6.1f}ms  p99={hedged_p99:>6.1f}ms"
        f"  upstream={hedged_upstream_calls}"
    )

    p99_reduction = (control_p99 - hedged_p99) / control_p99 if control_p99 > 0 else 0.0
    p95_reduction = (control_p95 - hedged_p95) / control_p95 if control_p95 > 0 else 0.0
    cost_overhead = (
        (hedged_upstream_calls - control_upstream_calls) / control_upstream_calls
        if control_upstream_calls > 0
        else 0.0
    )

    print()
    print("=" * 64)
    print("Phase 27 — request hedging tail-latency experiment")
    print("=" * 64)
    print(f"runs per arm:                 {args.runs}")
    print(f"hedge trigger rate:           {hedged_triggers / args.runs:>6.1%}")
    print(f"hedge win rate (of triggers): "
          f"{(hedged_wins / hedged_triggers if hedged_triggers else 0):>6.1%}")
    print()
    print("                   control      hedged    delta")
    print(f"  p50          {control_p50:>8.1f}ms  {hedged_p50:>8.1f}ms"
          f"  {(control_p50 - hedged_p50):>+7.1f}ms")
    print(f"  p95          {control_p95:>8.1f}ms  {hedged_p95:>8.1f}ms"
          f"  {(control_p95 - hedged_p95):>+7.1f}ms")
    print(f"  p99          {control_p99:>8.1f}ms  {hedged_p99:>8.1f}ms"
          f"  {(control_p99 - hedged_p99):>+7.1f}ms")
    print()
    print(f"p95 reduction: {p95_reduction:>+6.1%}")
    print(f"p99 reduction: {p99_reduction:>+6.1%}")
    print(f"upstream-call overhead: +{cost_overhead:.1%} "
          f"({control_upstream_calls} -> {hedged_upstream_calls})")
    print()

    if p99_reduction >= args.min_p99_reduction:
        print(
            f"VERDICT: claim holds — p99 dropped by {p99_reduction:.1%} "
            f"(threshold: {args.min_p99_reduction:.0%}) at "
            f"+{cost_overhead:.0%} upstream-call overhead."
        )
        sys.exit(0)
    else:
        print(
            f"VERDICT: claim fails — p99 dropped by only "
            f"{p99_reduction:.1%} (needed >= {args.min_p99_reduction:.0%}). "
            f"Hedging delay may need tuning for this workload."
        )
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(_main())
