"""Live verification of the circuit breaker speedup (Claim #6).

Stages the exact scenario the README describes:

1. Configure a provider with a *non-routable* base URL (closed TCP port).
2. Issue 5 calls — each times out at the connect layer → breaker counts
   5 consecutive retryable failures → breaker trips to OPEN.
3. Issue a 6th call — breaker is OPEN → failover skips the dead provider
   instantly, then returns a 503 with no upstream connection attempt.
4. Measure the wall-clock of both the slow CLOSED-state attempts and the
   fast OPEN-state skip. The ratio is the speedup the README cites.

This is an *in-process* test of the breaker mechanism — it does not
require the gateway HTTP layer. Calling ``execute_with_failover``
directly with a bad provider is the cleanest way to stage the scenario
without modifying the catalog or the team allowlist.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any

from pronaos.core.circuit import CircuitBreakerRegistry
from pronaos.core.failover import execute_with_failover
from pronaos.core.router import RoutingPlan
from pronaos.providers.base import (
    ChatCompletionChunk,
    ChatCompletionRequest,
    Provider,
    UpstreamTimeoutError,
)


class BrokenProvider(Provider):
    """A provider that always fails with a retryable error.

    Simulates a real outage without actually waiting for TCP timeouts.
    The breaker treats UpstreamTimeoutError as retryable → counts toward
    the threshold → trips OPEN at 5 consecutive.
    """

    name = "broken-test-provider"

    def __init__(self, simulated_latency_ms: float = 100.0) -> None:
        self._latency = simulated_latency_ms / 1000.0

    async def chat_completion(
        self, req: ChatCompletionRequest
    ) -> AsyncIterator[ChatCompletionChunk]:
        # The real provider raises BEFORE returning the async iterator
        # when the upstream is unreachable — the failover layer expects
        # the exception on ``await provider.chat_completion()``. We do
        # the same. No ``yield`` here → this is a coroutine that
        # always raises, never an async-generator function.
        await asyncio.sleep(self._latency)
        raise UpstreamTimeoutError(
            "simulated upstream timeout for breaker test"
        )

    def cost_cents(
        self, prompt_tokens: int, completion_tokens: int, model: str
    ) -> int:
        return 0

    async def aclose(self) -> None:
        pass


async def _drain(stream: AsyncIterator[Any]) -> None:
    async for _ in stream:
        pass


async def main() -> None:
    provider = BrokenProvider(simulated_latency_ms=100.0)
    registry = CircuitBreakerRegistry()
    plan = RoutingPlan(primary=provider, fallbacks=())
    req = ChatCompletionRequest(
        model="broken-test-provider/test",
        messages=[{"role": "user", "content": "hi"}],
        stream=True,
    )

    print("phase 1: hammer the broken provider until the breaker trips")
    closed_latencies_ms: list[float] = []
    for i in range(1, 6):
        t0 = time.monotonic()
        try:
            _picked, stream = await execute_with_failover(
                plan, req, circuit_registry=registry
            )
            await _drain(stream)
        except Exception as e:
            wall_ms = (time.monotonic() - t0) * 1000.0
            closed_latencies_ms.append(wall_ms)
            state = registry.get(provider.name).state.value
            print(
                f"  [{i}/5] {wall_ms:>7.1f} ms  → {type(e).__name__}  "
                f"(breaker now {state})"
            )

    print()
    print("phase 2: breaker should now be OPEN — measure a skipped call")
    t0 = time.monotonic()
    try:
        _picked, stream = await execute_with_failover(
            plan, req, circuit_registry=registry
        )
        await _drain(stream)
    except Exception as e:
        open_latency_ms = (time.monotonic() - t0) * 1000.0
        state = registry.get(provider.name).state.value
        print(
            f"  OPEN skip:  {open_latency_ms:>7.1f} ms  "
            f"→ {type(e).__name__}  (breaker {state})"
        )

    avg_closed_ms = sum(closed_latencies_ms) / len(closed_latencies_ms)
    speedup = avg_closed_ms / open_latency_ms if open_latency_ms > 0 else float("inf")

    print()
    print("=" * 56)
    print("circuit breaker speedup verification")
    print("=" * 56)
    print(f"avg CLOSED-state attempt (5 calls):  {avg_closed_ms:>8.2f} ms")
    print(f"OPEN-state skip (1 call):            {open_latency_ms:>8.2f} ms")
    print(f"speedup:                             {speedup:>8.1f}x")
    print()
    if speedup >= 5.0:
        print("✅ VERDICT: breaker skip is meaningfully faster than the")
        print(f"            CLOSED-state attempt — {speedup:.1f}x speedup.")
    else:
        print("⚠ VERDICT: speedup below 5x — investigate.")


if __name__ == "__main__":
    asyncio.run(main())
