"""Outbound webhook dispatcher.

Why webhooks alongside Prometheus
---------------------------------
Metrics on ``/metrics`` are useful for *dashboards* — humans actively
watching graphs. They're useless for *alerts* on events that require
immediate human attention: a tenant's quota is exhausted, the circuit
breaker for a provider just opened, the audit chain just broke.

Webhooks push those signals into the operator's incident-response
plumbing (Slack/PagerDuty/Opsgenie/custom). Each tenant configures
its own URL + shared secret; the dispatcher POSTs a JSON payload with
an HMAC-SHA256 signature so receivers can verify authenticity.

Design choices
--------------
- **Tenant-scoped**, not gateway-global. Each tenant gets one webhook;
  multi-tenant deployments don't want events from tenant A landing in
  tenant B's incident channel.
- **Fire-and-forget** at the call site. Publishing an event must NOT
  block the gateway's hot path. The dispatcher's ``publish()`` returns
  immediately; the actual HTTP POST runs in a background task.
- **Retry on transient failures.** 5xx + connection errors → up to 3
  retries with exponential backoff. 4xx → permanent failure (the
  receiver said our payload was bad; retrying won't help). Final
  failure is logged but never raised — the gateway must not 5xx
  because a tenant's webhook receiver is down.
- **HMAC-SHA256 signature** in the ``X-Pronaos-Signature`` header,
  same shape as GitHub webhooks (``sha256=<hex>``). Common, well-
  documented, and trivially verifiable on the receiver side with
  any HMAC library.
- **Compact payload schema.** ``{event, ts, tenant_id, data}`` —
  ``event`` and ``ts`` are top-level so receivers can dispatch
  without parsing the payload body; ``data`` is the event-specific
  bag.

Event types
-----------
Three publish-points are wired today:

- ``quota.exhausted`` — fired when ``enforce_quotas`` denies a request
  for token-budget or cost-budget exhaustion. Payload includes the
  reason, team, and budget context.
- ``circuit.tripped`` — fired when a provider's circuit breaker
  transitions CLOSED→OPEN. Payload includes the provider name and
  trip count.
- ``audit.chain_broken`` — fired by the verifier when a hash-chain
  break is detected. Payload includes the offending record id and
  the reason (hash_mismatch vs prev_hash_mismatch).

Adding a new event type = define a new dataclass + call ``publish()``
from the appropriate publish-point. The dispatcher itself stays
agnostic.

Threading + lifecycle
---------------------
The dispatcher uses ``asyncio.create_task`` for the background POST.
Tasks are tracked in a strong-reference set on the dispatcher so
asyncio's garbage collector doesn't kill them mid-flight. The
dispatcher's ``aclose()`` awaits all pending tasks — used at shutdown
to make sure no events go missing.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from dataclasses import dataclass, field
from typing import Any, Final, Literal

import httpx

from pronaos.logging import get_logger

log = get_logger(__name__)


# Header names the dispatcher emits, exposed as constants so tests and
# receiver-side libraries can pin them.
SIGNATURE_HEADER: Final = "X-Pronaos-Signature"
EVENT_HEADER: Final = "X-Pronaos-Event"
DELIVERY_HEADER: Final = "X-Pronaos-Delivery"


# Retry tuning. The 5xx + connection-error retry budget is intentionally
# small — webhooks are best-effort by design; a chronically-failing
# receiver shouldn't keep retrying forever and consuming the gateway's
# task budget.
DEFAULT_MAX_ATTEMPTS: Final = 3
DEFAULT_BACKOFF_BASE_SECONDS: Final = 0.5  # 0.5, 1.0, 2.0 …


EventType = Literal[
    "quota.exhausted",
    "circuit.tripped",
    "audit.chain_broken",
]


@dataclass(frozen=True, slots=True)
class WebhookConfig:
    """Per-tenant webhook config, pulled from Tenant.webhook_url + .webhook_secret.

    Both fields must be set for the dispatcher to fire. Either being
    None makes the dispatcher a no-op for this tenant. (Validated at
    publish() time, not at construction, so a tenant can clear the
    config without restarting the gateway.)"""

    url: str | None
    secret: str | None


@dataclass(frozen=True, slots=True)
class WebhookEvent:
    """One event payload. ``event`` and ``ts`` go in headers AND the body
    so receivers can route purely from headers if they want to.

    ``data`` is the event-specific payload; shape varies by event type
    but always serializes cleanly to JSON.

    ``delivery_id`` is a fresh UUID per delivery attempt (not per event)
    so retries get distinct delivery IDs — receivers doing idempotency
    keyed on delivery_id won't dedup retries (correct behavior; each
    POST is a fresh delivery attempt of the same logical event)."""

    event: EventType
    tenant_id: str
    data: dict[str, Any]
    ts: float = field(default_factory=time.time)

    def to_body(self) -> dict[str, Any]:
        """Serialize to the JSON the receiver sees."""
        return {
            "event": self.event,
            "ts": self.ts,
            "tenant_id": self.tenant_id,
            "data": self.data,
        }


def sign_payload(body: bytes, secret: str) -> str:
    """Compute the HMAC-SHA256 signature for a payload.

    Returns the value to put in the ``X-Pronaos-Signature`` header:
    ``"sha256=<hex>"`` — matching the GitHub webhook convention so
    receivers can use existing libraries.

    Exposed publicly so test code (and receiver-side reference impls)
    can compute the expected signature without coupling to the
    dispatcher class.
    """
    mac = hmac.new(secret.encode("utf-8"), body, hashlib.sha256)
    return f"sha256={mac.hexdigest()}"


class WebhookDispatcher:
    """Publishes events to a tenant's configured webhook URL.

    Single instance per gateway process, shared across requests.
    Holds the httpx client so we don't pay TLS handshake cost on
    every event."""

    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 10.0,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        backoff_base_seconds: float = DEFAULT_BACKOFF_BASE_SECONDS,
    ) -> None:
        self._owns_client = http_client is None
        self._http = http_client or httpx.AsyncClient(timeout=timeout_seconds)
        self._max_attempts = max_attempts
        self._backoff_base = backoff_base_seconds
        # Strong references to in-flight delivery tasks. Without this,
        # asyncio can GC the task mid-flight and silently drop the
        # event. Tasks remove themselves from the set on completion.
        self._pending: set[asyncio.Task[None]] = set()

    def publish(self, config: WebhookConfig, event: WebhookEvent) -> None:
        """Schedule an asynchronous delivery for ``event``.

        Returns immediately. If the tenant has no webhook configured,
        this is a no-op (no logging — webhooks-not-configured is the
        common case and shouldn't fill logs)."""
        if not config.url or not config.secret:
            return
        # asyncio.create_task requires an event loop. If we're called
        # from a non-async context (which shouldn't happen, but is
        # easy to assert against), there's no loop — bail.
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            log.warning(
                "webhook.publish_no_event_loop",
                event_type=event.event,
                tenant_id=event.tenant_id,
            )
            return

        task = loop.create_task(self._deliver(config, event))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def aclose(self) -> None:
        """Wait for in-flight deliveries to finish, then close the HTTP client.

        Called at app shutdown. A short grace period is acceptable — we
        DON'T want to lose events on graceful shutdown, but a hung
        receiver shouldn't block the gateway from terminating. The
        outer shutdown handler can ``wait_for(aclose(), timeout=...)``
        if it wants to enforce a deadline."""
        if self._pending:
            await asyncio.gather(*self._pending, return_exceptions=True)
        if self._owns_client:
            await self._http.aclose()

    # ------------------------------------------------------------------ #
    # Internal                                                            #
    # ------------------------------------------------------------------ #

    async def _deliver(self, config: WebhookConfig, event: WebhookEvent) -> None:
        """Single delivery attempt with retry-on-transient-failure.

        Logged on every attempt outcome (success / retry / give-up) so
        operators can grep the log for one delivery's lifecycle. Never
        raises — webhook delivery is best-effort by design.
        """
        body = json.dumps(event.to_body(), separators=(",", ":")).encode("utf-8")
        signature = sign_payload(body, config.secret or "")

        # Build a fresh delivery_id per attempt — receivers doing
        # idempotency would dedupe on this, which we DON'T want
        # (each retry should be processed in case the prior delivery
        # was lost). uuid.uuid4 is fine; the value just needs to be
        # opaque + unique to the operator's eyes.
        import uuid

        for attempt in range(1, self._max_attempts + 1):
            delivery_id = uuid.uuid4().hex
            headers = {
                "content-type": "application/json",
                EVENT_HEADER: event.event,
                SIGNATURE_HEADER: signature,
                DELIVERY_HEADER: delivery_id,
            }
            try:
                resp = await self._http.post(
                    config.url or "",  # url not None per publish() check
                    content=body,
                    headers=headers,
                )
            except httpx.RequestError as e:
                log.warning(
                    "webhook.delivery_network_error",
                    event_type=event.event,
                    tenant_id=event.tenant_id,
                    attempt=attempt,
                    error=str(e),
                )
                if attempt < self._max_attempts:
                    await asyncio.sleep(self._backoff_base * (2 ** (attempt - 1)))
                    continue
                log.error(
                    "webhook.delivery_failed_terminal",
                    event_type=event.event,
                    tenant_id=event.tenant_id,
                    reason="network_error",
                    final_error=str(e),
                )
                return

            if 200 <= resp.status_code < 300:
                log.info(
                    "webhook.delivered",
                    event_type=event.event,
                    tenant_id=event.tenant_id,
                    status=resp.status_code,
                    attempt=attempt,
                )
                return

            # 4xx → no retry (client error in our payload; retrying
            # won't help). 5xx → retry.
            if 400 <= resp.status_code < 500:
                log.error(
                    "webhook.delivery_rejected",
                    event_type=event.event,
                    tenant_id=event.tenant_id,
                    status=resp.status_code,
                    body_preview=resp.text[:200],
                )
                return

            log.warning(
                "webhook.delivery_5xx",
                event_type=event.event,
                tenant_id=event.tenant_id,
                attempt=attempt,
                status=resp.status_code,
            )
            if attempt < self._max_attempts:
                await asyncio.sleep(self._backoff_base * (2 ** (attempt - 1)))

        log.error(
            "webhook.delivery_failed_terminal",
            event_type=event.event,
            tenant_id=event.tenant_id,
            reason="5xx_exhausted",
        )


# --------------------------------------------------------------------------- #
# Convenience event constructors                                              #
# --------------------------------------------------------------------------- #
#
# One factory per event type — keeps the publish-points readable and the
# payload schemas centralised. Adding a new event type means adding a new
# factory + bumping the EventType literal at module top.


def quota_exhausted_event(
    *,
    tenant_id: str,
    team_id: str,
    team_name: str,
    reason: str,
    retry_after_seconds: int | None,
) -> WebhookEvent:
    """Construct a ``quota.exhausted`` event payload.

    ``reason`` is the QuotaTracker's denial reason — one of
    ``monthly_token_budget_exhausted`` / ``monthly_cost_budget_exhausted``
    / ``rate_limit_exceeded``."""
    return WebhookEvent(
        event="quota.exhausted",
        tenant_id=tenant_id,
        data={
            "team_id": team_id,
            "team_name": team_name,
            "reason": reason,
            "retry_after_seconds": retry_after_seconds,
        },
    )


def circuit_tripped_event(
    *,
    tenant_id: str,
    provider: str,
    trip_count: int,
) -> WebhookEvent:
    """Construct a ``circuit.tripped`` event payload.

    Fired exactly once per CLOSED→OPEN (or HALF_OPEN→OPEN) transition,
    matching the ``pronaos_circuit_trips_total`` counter. ``trip_count``
    is the cumulative number of trips for this provider since process
    start — useful for receivers to dedupe rapid re-trips."""
    return WebhookEvent(
        event="circuit.tripped",
        tenant_id=tenant_id,
        data={
            "provider": provider,
            "trip_count": trip_count,
        },
    )


def audit_chain_broken_event(
    *,
    tenant_id: str,
    record_id: str,
    reason: str,
    ts: str | None,
) -> WebhookEvent:
    """Construct an ``audit.chain_broken`` event payload.

    Fired by the audit verifier when a hash-chain integrity check
    detects tampering. ``reason`` is ``hash_mismatch`` (the stored
    this_hash doesn't match recomputed) or ``prev_hash_mismatch``
    (the prev_hash pointer is wrong). ``ts`` is the offending record's
    timestamp in ISO format."""
    return WebhookEvent(
        event="audit.chain_broken",
        tenant_id=tenant_id,
        data={
            "record_id": record_id,
            "reason": reason,
            "record_ts": ts,
        },
    )
