"""Failover executor.

Walks a routing plan, calling providers in order until one succeeds or the
error is non-retryable. Composes with the circuit breaker (``core/circuit.py``)
for cross-request health state: this layer is per-request best-effort retry
along the pre-computed chain; the breaker remembers persistent degradation
between requests and skips known-bad providers up front.

Streaming note
--------------
Fallback is *only* tried before the first byte of the response body leaves
the provider. Once a provider has started streaming tokens, we commit to it —
swapping mid-stream would produce corrupted output on the client side.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from pronaos.core.circuit import CircuitBreakerRegistry
from pronaos.core.router import RoutingPlan
from pronaos.logging import get_logger
from pronaos.observability.metrics import record_circuit_skipped, record_circuit_trip
from pronaos.providers.base import (
    AuthError,
    ChatCompletionChunk,
    ChatCompletionRequest,
    Provider,
    ProviderError,
)

log = get_logger(__name__)


class AllProvidersFailedError(ProviderError):
    """Every provider in the chain failed. Carries the last error."""

    retryable = False
    status = 502


async def execute_with_failover(
    plan: RoutingPlan,
    req: ChatCompletionRequest,
    *,
    circuit_registry: CircuitBreakerRegistry | None = None,
) -> tuple[Provider, AsyncIterator[ChatCompletionChunk]]:
    """Return the first provider that successfully **started** the response.

    For non-streaming calls this means the HTTP call completed; for streaming
    it means headers were received without error. The caller iterates the
    returned stream to consume tokens.

    ``circuit_registry`` is the per-provider breaker registry. When
    supplied, providers whose breaker is OPEN are skipped entirely
    (we don't even try the call), and the breaker is updated based on
    the call outcome:

    - Success → record_success (resets streak, closes if HALF_OPEN)
    - ProviderError that's retryable → record_failure (trips on streak)
    - AuthError (non-retryable) → NOT recorded — auth misconfiguration
      isn't a health signal about the upstream and shouldn't ever trip
      a circuit. (A bad key won't get healthier with a 30s wait.)

    The argument is optional so unit tests that don't care about the
    breaker can skip wiring it. In the live app it's always supplied.
    """
    last_error: Exception | None = None

    for attempt_idx, provider in enumerate(plan.chain()):
        breaker = circuit_registry.get(provider.name) if circuit_registry else None

        if breaker is not None and not breaker.allow_request():
            # Circuit OPEN — skip this provider entirely. Count it so
            # dashboards can correlate "saved upstream calls" with
            # breaker openings.
            record_circuit_skipped(provider.name)
            log.info(
                "failover.circuit_open_skip",
                provider=provider.name,
                attempt=attempt_idx,
                state=breaker.state.value,
            )
            continue

        try:
            stream = await provider.chat_completion(req)
        except AuthError as e:
            # Auth errors are not a provider-health signal. They mean the
            # gateway is misconfigured; the next provider might have a
            # working key, but the broken one isn't going to recover by
            # itself. Skip the breaker update entirely and let the
            # non-retryable raise path handle it.
            last_error = e
            log.warning(
                "failover.attempt_failed",
                provider=provider.name,
                attempt=attempt_idx,
                retryable=False,
                status=e.status,
                error=str(e),
            )
            raise
        except ProviderError as e:
            last_error = e
            if breaker is not None:
                trips_before = breaker.trip_count
                breaker.record_failure()
                # If this failure crossed the threshold (or was a
                # HALF_OPEN re-open), trip_count just incremented.
                # Forward to the metric so the "trips" counter
                # reflects discrete trip events, not every failure.
                if breaker.trip_count > trips_before:
                    record_circuit_trip(provider.name)
            log.warning(
                "failover.attempt_failed",
                provider=provider.name,
                attempt=attempt_idx,
                retryable=e.retryable,
                status=e.status,
                error=str(e),
                circuit_state=breaker.state.value if breaker else None,
            )
            if not e.retryable:
                # Non-retryable, non-auth: bad request, unknown provider.
                # No point continuing — same input, different upstream
                # will fail the same way.
                raise
            continue

        # Successful start. Mark the breaker healthy and return the stream.
        # Note: we treat "stream started" as success. A mid-stream error
        # cannot retrospectively trip the breaker because we've already
        # committed to this provider and shipped headers to the client.
        if breaker is not None:
            breaker.record_success()
        if attempt_idx > 0:
            log.info(
                "failover.succeeded",
                provider=provider.name,
                skipped=attempt_idx,
            )
        return provider, stream

    assert last_error is not None
    raise AllProvidersFailedError(
        f"all providers in chain failed; last error: {last_error}"
    ) from last_error
