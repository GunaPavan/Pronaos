"""Failover executor.

Walks a routing plan, calling providers in order until one succeeds or the
error is non-retryable. Distinct from phase-9's circuit breaker — this is a
per-request best-effort retry along the pre-computed chain; the breaker is
across-requests state.

Streaming note
--------------
Fallback is *only* tried before the first byte of the response body leaves
the provider. Once a provider has started streaming tokens, we commit to it —
swapping mid-stream would produce corrupted output on the client side.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from pronaos.core.router import RoutingPlan
from pronaos.logging import get_logger
from pronaos.providers.base import (
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
    plan: RoutingPlan, req: ChatCompletionRequest
) -> tuple[Provider, AsyncIterator[ChatCompletionChunk]]:
    """Return the first provider that successfully **started** the response.

    For non-streaming calls this means the HTTP call completed; for streaming
    it means headers were received without error. The caller iterates the
    returned stream to consume tokens.
    """
    last_error: Exception | None = None

    for attempt_idx, provider in enumerate(plan.chain()):
        try:
            stream = await provider.chat_completion(req)
        except ProviderError as e:
            last_error = e
            log.warning(
                "failover.attempt_failed",
                provider=provider.name,
                attempt=attempt_idx,
                retryable=e.retryable,
                status=e.status,
                error=str(e),
            )
            if not e.retryable:
                # Auth failures, bad requests, unknown providers: no point
                # retrying — they'll fail the same way on any other provider.
                raise
            continue
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
