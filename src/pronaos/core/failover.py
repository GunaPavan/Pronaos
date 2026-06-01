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

Hedging (Phase 27)
------------------
Optional **speculative parallel start**. When the caller passes
``hedge_delay_ms``, the executor races the primary against the first
fallback: start the primary, wait ``hedge_delay_ms``, and if the primary
hasn't returned yet, start the next chain provider in parallel and return
whichever finishes first. The loser is cancelled (its httpx stream is
closed). This trades a fractional cost overhead for p99 latency reduction
(Dean & Barroso, "The Tail at Scale", CACM 2013).

Hedging respects the breaker — a hedge candidate whose breaker is OPEN is
skipped and the executor falls through to the next provider. The hedge
NEVER starts before the delay elapses, and at most ``hedge_max_count``
hedges fire per request (default 1).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextvars import ContextVar
from dataclasses import dataclass

from pronaos.core.circuit import CircuitBreakerRegistry
from pronaos.core.router import RoutingPlan
from pronaos.logging import get_logger
from pronaos.observability.metrics import (
    record_circuit_skipped,
    record_circuit_trip,
    record_hedge_cancelled,
    record_hedge_triggered,
    record_hedge_win,
)
from pronaos.providers.base import (
    AuthError,
    ChatCompletionChunk,
    ChatCompletionRequest,
    Provider,
    ProviderError,
)

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class HedgeOutcome:
    """Per-request hedging summary the chat handler stamps into response headers.

    ``triggered`` means a hedge was actually started (the primary did not
    return within ``hedge_delay_ms``). ``winner_role`` is ``primary`` when
    the original target beat the hedge, ``hedge`` when the speculative
    call won, or ``None`` when no hedge fired (sequential failover path).
    """

    triggered: bool = False
    winner_role: str | None = None
    winner_provider: str | None = None
    hedge_provider: str | None = None


# Set by the failover executor mid-request so the chat handler can stamp
# ``X-Pronaos-Hedged`` and ``X-Pronaos-Hedge-Winner`` response headers
# without changing the (provider, stream) return shape. Reset to a
# default-empty outcome at the top of every failover call so leftover
# state from a prior call in the same task can't leak across requests.
# Sentinel is the frozen ``_EMPTY_HEDGE_OUTCOME`` — frozen=True on the
# dataclass already makes it immutable, but B039 still flags any object
# default; the constant + ``.get(_EMPTY_HEDGE_OUTCOME)`` pattern keeps
# the lint clean without changing semantics.
_EMPTY_HEDGE_OUTCOME = HedgeOutcome()
hedge_outcome_var: ContextVar[HedgeOutcome] = ContextVar("pronaos_hedge_outcome")


class AllProvidersFailedError(ProviderError):
    """Every provider in the chain failed. Carries the last error."""

    retryable = False
    status = 502


# Sentinel for "primary returned an error" inside the hedge race — kept
# private to this module. Wrapping the error in a dataclass-style object
# would be overkill; an exception-as-value is enough because we only use
# it to distinguish "primary failed, fall through to next" from "primary
# is still running, await it."
class _PrimaryFailed(Exception):
    """Internal: primary raised before the hedge had a chance to run."""

    def __init__(self, original: BaseException) -> None:
        super().__init__(str(original))
        self.original = original


async def execute_with_failover(
    plan: RoutingPlan,
    req: ChatCompletionRequest,
    *,
    circuit_registry: CircuitBreakerRegistry | None = None,
    hedge_delay_ms: float | None = None,
    hedge_max_count: int = 1,
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

    ``hedge_delay_ms`` (Phase 27): when set to a positive number, the
    executor races the primary against the next chain provider. The
    hedge starts only if the primary hasn't returned within this many
    milliseconds. ``None`` or ``0.0`` reverts to sequential failover.

    ``hedge_max_count`` caps how many hedges may fire per request,
    regardless of chain length. Default 1 — race the primary against
    one alternative. Set to 0 to disable hedging even when
    ``hedge_delay_ms`` is set.

    The argument is optional so unit tests that don't care about the
    breaker can skip wiring it. In the live app it's always supplied.
    """
    chain = plan.chain()
    hedging_enabled = (
        hedge_delay_ms is not None
        and hedge_delay_ms > 0.0
        and hedge_max_count > 0
        and len(chain) >= 2
    )

    # Reset the per-request hedge outcome at the top so a leftover from
    # a prior call in the same asyncio task can't leak into headers.
    hedge_outcome_var.set(_EMPTY_HEDGE_OUTCOME)

    if not hedging_enabled:
        return await _sequential_failover(chain, req, circuit_registry=circuit_registry)

    return await _hedged_failover(
        chain,
        req,
        circuit_registry=circuit_registry,
        hedge_delay_seconds=hedge_delay_ms / 1000.0 if hedge_delay_ms else 0.0,
        hedge_max_count=hedge_max_count,
    )


# --------------------------------------------------------------------------- #
# Sequential failover (the long-standing path, factored out for clarity)      #
# --------------------------------------------------------------------------- #


async def _sequential_failover(
    chain: tuple[Provider, ...],
    req: ChatCompletionRequest,
    *,
    circuit_registry: CircuitBreakerRegistry | None,
) -> tuple[Provider, AsyncIterator[ChatCompletionChunk]]:
    """Walk the chain in order; return the first success."""
    last_error: Exception | None = None

    for attempt_idx, provider in enumerate(chain):
        breaker = circuit_registry.get(provider.name) if circuit_registry else None

        if breaker is not None and not breaker.allow_request():
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
                raise
            continue

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


# --------------------------------------------------------------------------- #
# Hedged failover (Phase 27)                                                  #
# --------------------------------------------------------------------------- #


async def _hedged_failover(
    chain: tuple[Provider, ...],
    req: ChatCompletionRequest,
    *,
    circuit_registry: CircuitBreakerRegistry | None,
    hedge_delay_seconds: float,
    hedge_max_count: int,
) -> tuple[Provider, AsyncIterator[ChatCompletionChunk]]:
    """Race primary against next chain provider, return the winner.

    Algorithm:

    1. Pick the first non-OPEN-breaker provider as ``primary``. Start
       its call as task A.
    2. Wait ``hedge_delay_seconds``. If A returned (success or error)
       within that window, handle normally and return.
    3. Otherwise, pick the next non-OPEN-breaker provider, record a
       hedge-triggered event, and start its call as task B.
    4. ``asyncio.wait([A, B], FIRST_COMPLETED)``. The first one to
       finish is the winner:
       - If winner returned success → cancel loser, return winner.
       - If winner returned error → wait for the other one; if both
         error, fall through to the next provider not yet attempted.

    A hedge that finishes faster than the loser still counts as
    consuming one upstream attempt (the loser was already in-flight).
    This is the honest cost overhead of hedging — we report it via
    ``pronaos_hedge_cancelled_total``.
    """
    eligible = [p for p in chain if _provider_allowed(p, circuit_registry)]
    if not eligible:
        # Every chain provider has an OPEN breaker — fall through to
        # the sequential path so it records the skip metrics and
        # raises AllProvidersFailedError.
        return await _sequential_failover(chain, req, circuit_registry=circuit_registry)

    primary = eligible[0]
    hedges_remaining = min(hedge_max_count, len(eligible) - 1)

    # Account for circuit-OPEN providers earlier in the chain that we
    # skipped — keep their skip-metric ticks consistent.
    for skipped in chain[: chain.index(primary)]:
        breaker = circuit_registry.get(skipped.name) if circuit_registry else None
        if breaker is not None and not breaker.allow_request():
            record_circuit_skipped(skipped.name)
            log.info(
                "failover.circuit_open_skip",
                provider=skipped.name,
                attempt=0,
                state=breaker.state.value,
            )

    primary_task = asyncio.create_task(
        _safe_call(primary, req), name=f"hedge-primary-{primary.name}"
    )

    # Wait for primary to either finish or timeout.
    done, _pending = await asyncio.wait(
        {primary_task}, timeout=hedge_delay_seconds, return_when=asyncio.FIRST_COMPLETED
    )

    if primary_task in done:
        # Primary finished inside the hedge window — no hedge needed.
        return await _resolve_winner(
            primary_task,
            None,
            primary_name=primary.name,
            hedge_name=None,
            circuit_registry=circuit_registry,
            chain=chain,
            req=req,
            already_tried={primary},
        )

    # Hedge fires. Pick the next eligible provider; skip those whose
    # breaker has flipped to OPEN since the start of the request (rare
    # but possible during a thundering outage).
    hedge: Provider | None = None
    for candidate in eligible[1:]:
        if _provider_allowed(candidate, circuit_registry):
            hedge = candidate
            break

    if hedge is None or hedges_remaining <= 0:
        # No eligible hedge candidate — just await the primary to its
        # natural conclusion.
        return await _resolve_winner(
            primary_task,
            None,
            primary_name=primary.name,
            hedge_name=None,
            circuit_registry=circuit_registry,
            chain=chain,
            req=req,
            already_tried={primary},
        )

    record_hedge_triggered(primary=primary.name, hedge=hedge.name)
    # Remember the hedge details so the chat handler can stamp response
    # headers regardless of who wins the race — flagging "we tried" is
    # useful even when the primary still wins (operators tuning
    # ``hedge_delay_ms`` want to see "the hedge fired but didn't help").
    hedge_outcome_var.set(HedgeOutcome(triggered=True, hedge_provider=hedge.name))
    log.info(
        "hedge.triggered",
        primary=primary.name,
        hedge=hedge.name,
        delay_ms=hedge_delay_seconds * 1000.0,
    )

    hedge_task = asyncio.create_task(_safe_call(hedge, req), name=f"hedge-alt-{hedge.name}")

    return await _resolve_winner(
        primary_task,
        hedge_task,
        primary_name=primary.name,
        hedge_name=hedge.name,
        circuit_registry=circuit_registry,
        chain=chain,
        req=req,
        already_tried={primary, hedge},
    )


def _provider_allowed(provider: Provider, registry: CircuitBreakerRegistry | None) -> bool:
    """Return True if the breaker for ``provider`` is not OPEN."""
    if registry is None:
        return True
    breaker = registry.get(provider.name)
    return breaker.allow_request()


async def _safe_call(
    provider: Provider, req: ChatCompletionRequest
) -> tuple[Provider, AsyncIterator[ChatCompletionChunk]]:
    """Call ``provider.chat_completion`` and tag the result with the provider.

    Returning the provider alongside the stream lets the race resolver
    identify which task won without having to remember the mapping
    externally.
    """
    stream = await provider.chat_completion(req)
    return provider, stream


async def _resolve_winner(
    primary_task: asyncio.Task[tuple[Provider, AsyncIterator[ChatCompletionChunk]]],
    hedge_task: asyncio.Task[tuple[Provider, AsyncIterator[ChatCompletionChunk]]] | None,
    *,
    primary_name: str,
    hedge_name: str | None,
    circuit_registry: CircuitBreakerRegistry | None,
    chain: tuple[Provider, ...],
    req: ChatCompletionRequest,
    already_tried: set[Provider],
) -> tuple[Provider, AsyncIterator[ChatCompletionChunk]]:
    """Wait on the in-flight task(s), return the winner, cancel the loser.

    The hedge case has both tasks live; if both eventually error we
    walk the remaining chain sequentially with the sequential helper
    (this preserves the chain-walking semantics for chain length > 2).
    """
    if hedge_task is None:
        # No hedge — just resolve the primary's outcome.
        try:
            winner_provider, stream = await primary_task
        except (AuthError, ProviderError) as e:
            await _record_failure(primary_name, e, circuit_registry)
            if isinstance(e, AuthError) or not e.retryable:
                raise
            return await _fall_through(chain, req, circuit_registry, already_tried, e)

        await _record_success(winner_provider.name, circuit_registry)
        return winner_provider, stream

    # Race the two tasks. The first one to complete is the candidate
    # winner; if it errored, we still wait on the loser.
    done, pending = await asyncio.wait(
        {primary_task, hedge_task}, return_when=asyncio.FIRST_COMPLETED
    )
    first = next(iter(done))
    # In rare cases both tasks complete inside the same scheduler tick
    # (e.g. both raise immediately from a synchronous exception path),
    # in which case ``done`` has both and ``pending`` is empty. Treat
    # the other ``done`` entry as the "other" candidate so its
    # exception (or result) is still consumed; otherwise we leak an
    # "exception was never retrieved" warning.
    other_done = [t for t in done if t is not first]

    try:
        winner_provider, stream = await first
    except (AuthError, ProviderError) as e:
        await _record_failure(
            _task_provider_name(first, primary_name, hedge_name), e, circuit_registry
        )
        # First finisher errored — the other task might still succeed.
        if pending or other_done:
            other = next(iter(pending)) if pending else other_done[0]
            try:
                winner_provider, stream = await other
            except (AuthError, ProviderError) as e2:
                await _record_failure(
                    _task_provider_name(other, primary_name, hedge_name), e2, circuit_registry
                )
                # Both errored. Fall through.
                if isinstance(e, AuthError) or isinstance(e2, AuthError):
                    raise
                if not e.retryable and not e2.retryable:
                    raise
                return await _fall_through(chain, req, circuit_registry, already_tried, e2)
            else:
                # The "loser" task became the winner.
                role = "primary" if other is primary_task else "hedge"
                record_hedge_win(winner_provider=winner_provider.name, role=role)
                _stamp_hedge_winner(role, winner_provider.name)
                # The "first finisher" we already counted as a failure;
                # nothing to cancel because it's done.
                await _record_success(winner_provider.name, circuit_registry)
                return winner_provider, stream
        else:
            # No other task — propagate.
            if isinstance(e, AuthError) or not e.retryable:
                raise
            return await _fall_through(chain, req, circuit_registry, already_tried, e)

    # First finisher succeeded — cancel the loser if it's still pending,
    # or consume its already-completed result if it finished in the same
    # tick (drains "exception was never retrieved" warnings).
    role = "primary" if first is primary_task else "hedge"
    record_hedge_win(winner_provider=winner_provider.name, role=role)
    _stamp_hedge_winner(role, winner_provider.name)
    for already_finished in other_done:
        # Drain the already-completed loser. Exception or success, we've
        # already committed to ``first`` so the loser's outcome is just
        # consumed to keep the event loop tidy.
        try:
            await already_finished
        except (asyncio.CancelledError, AuthError, ProviderError):
            pass
        except Exception as e:  # pragma: no cover — defensive
            log.warning("hedge.loser_unexpected_error", error=str(e))
    if pending:
        loser_task = next(iter(pending))
        loser_name = _task_provider_name(loser_task, primary_name, hedge_name)
        loser_task.cancel()
        record_hedge_cancelled(loser_name)
        # Drain the cancellation so the event loop doesn't surface a
        # CancelledError as an "unawaited" warning. We deliberately
        # swallow exceptions here — the cancelled task may have raced
        # the cancel and returned a real stream, but we've already
        # committed to ``first`` so we can't use it.
        try:
            await loser_task
        except (asyncio.CancelledError, AuthError, ProviderError):
            # Expected outcomes for a cancelled loser — the cancel may
            # race the upstream HTTP, so we get either CancelledError
            # (clean tear-down) or the underlying provider error if it
            # finished before the cancel reached the awaiter.
            pass
        except Exception as e:  # pragma: no cover — defensive
            # Any other exception is unexpected: the loser task should
            # never raise BaseException-subclasses other than the ones
            # above. Log it so an operator notices but don't crash the
            # request — we already have the winner's stream.
            log.warning("hedge.loser_unexpected_error", error=str(e))
        log.info(
            "hedge.cancelled_loser",
            winner=winner_provider.name,
            cancelled=loser_name,
        )

    await _record_success(winner_provider.name, circuit_registry)
    return winner_provider, stream


def _stamp_hedge_winner(role: str, winner_provider: str) -> None:
    """Update the per-request hedge outcome with the resolved winner.

    Preserves whatever ``triggered`` / ``hedge_provider`` was set when
    the hedge first fired; only the winner-side fields are filled in
    here. Called from inside ``_resolve_winner`` for both race outcomes
    (first-finisher-wins and other-task-rescues-it).
    """
    current = hedge_outcome_var.get(_EMPTY_HEDGE_OUTCOME)
    hedge_outcome_var.set(
        HedgeOutcome(
            triggered=current.triggered,
            winner_role=role,
            winner_provider=winner_provider,
            hedge_provider=current.hedge_provider,
        )
    )


def _task_provider_name(
    task: asyncio.Task[tuple[Provider, AsyncIterator[ChatCompletionChunk]]],
    primary_name: str,
    hedge_name: str | None,
) -> str:
    """Recover the provider name from a task's ``name`` field.

    We embed the provider name in the task name when we create it so
    that even an errored task can be attributed to the right upstream
    for metrics — we can't ``await`` an errored task and read the
    return value.
    """
    if task.get_name().endswith(primary_name):
        return primary_name
    if hedge_name is not None and task.get_name().endswith(hedge_name):
        return hedge_name
    # Fallback (shouldn't happen in practice).
    return primary_name


async def _record_success(provider_name: str, registry: CircuitBreakerRegistry | None) -> None:
    if registry is None:
        return
    breaker = registry.get(provider_name)
    breaker.record_success()


async def _record_failure(
    provider_name: str,
    err: BaseException,
    registry: CircuitBreakerRegistry | None,
) -> None:
    """Mirror the sequential path's breaker update for one failure."""
    if isinstance(err, AuthError):
        return  # auth errors don't reflect upstream health
    if registry is None:
        return
    breaker = registry.get(provider_name)
    trips_before = breaker.trip_count
    breaker.record_failure()
    if breaker.trip_count > trips_before:
        record_circuit_trip(provider_name)


async def _fall_through(
    chain: tuple[Provider, ...],
    req: ChatCompletionRequest,
    registry: CircuitBreakerRegistry | None,
    already_tried: set[Provider],
    last_error: BaseException,
) -> tuple[Provider, AsyncIterator[ChatCompletionChunk]]:
    """Both racing providers errored — walk the rest of the chain sequentially.

    The remaining chain (providers not yet attempted) is given to
    ``_sequential_failover``. If that also exhausts the chain we wrap
    the original ``last_error`` so the caller's exception message
    still identifies the race outcome.
    """
    remaining = tuple(p for p in chain if p not in already_tried)
    if not remaining:
        raise AllProvidersFailedError(
            f"all hedged providers failed; last error: {last_error}"
        ) from last_error
    try:
        return await _sequential_failover(remaining, req, circuit_registry=registry)
    except AllProvidersFailedError as e:
        raise AllProvidersFailedError(
            f"all providers failed (hedged + sequential fallback); last error: {last_error}"
        ) from e
