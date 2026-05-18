"""WebhookDispatcher tests.

Three layers covered here:

1. **Signing** — ``sign_payload`` produces the canonical
   ``sha256=<hex>`` value receivers will check. Round-trip with a
   hand-computed HMAC.

2. **Dispatcher** — fire a single event, observe the receiver gets
   the expected URL + headers + signed body. Mocked with respx; the
   asyncio task lifecycle is awaited via ``aclose()``.

3. **Retry + give-up policy** — 5xx triggers retries up to
   ``max_attempts``; 4xx aborts immediately; network errors retry the
   same way as 5xx.

End-to-end "trip the circuit, see the POST land" tests live in
test_streaming_coverage.py (or whichever fixture has full app state)
once the publish-points are wired. These tests stay focused on the
primitive.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json

import httpx
import pytest
import respx

from pronaos.core.webhooks import (
    DELIVERY_HEADER,
    EVENT_HEADER,
    SIGNATURE_HEADER,
    WebhookConfig,
    WebhookDispatcher,
    audit_chain_broken_event,
    circuit_tripped_event,
    quota_exhausted_event,
    sign_payload,
)

# --------------------------------------------------------------------------- #
# sign_payload                                                                 #
# --------------------------------------------------------------------------- #


def test_sign_payload_returns_sha256_hex_prefix() -> None:
    """Format must be ``sha256=<64-hex-chars>`` so receivers can use
    existing GitHub-style HMAC libraries verbatim."""
    sig = sign_payload(b"hello", "secret123")
    assert sig.startswith("sha256=")
    hex_part = sig.removeprefix("sha256=")
    assert len(hex_part) == 64
    int(hex_part, 16)  # raises if non-hex


def test_sign_payload_matches_handcomputed_hmac() -> None:
    """Round-trip with a manually-computed HMAC. If this test ever fails,
    the signing function changed in a backwards-incompatible way and
    receivers will reject every delivery."""
    body = b'{"event":"test"}'
    secret = "supersecret"
    expected_hex = hmac.new(
        secret.encode("utf-8"), body, hashlib.sha256
    ).hexdigest()

    assert sign_payload(body, secret) == f"sha256={expected_hex}"


def test_different_secrets_produce_different_signatures() -> None:
    """Sanity check: signing is actually keyed on the secret. A bug where
    we ignored the secret would still produce hex-shaped output but
    every payload would have the same hash."""
    body = b"payload"
    s1 = sign_payload(body, "secret1")
    s2 = sign_payload(body, "secret2")
    assert s1 != s2


# --------------------------------------------------------------------------- #
# Event factories                                                              #
# --------------------------------------------------------------------------- #


def test_quota_exhausted_event_schema() -> None:
    ev = quota_exhausted_event(
        tenant_id="t1",
        team_id="team1",
        team_name="engineering",
        reason="monthly_token_budget_exhausted",
        retry_after_seconds=1234,
    )
    assert ev.event == "quota.exhausted"
    assert ev.tenant_id == "t1"
    assert ev.data["reason"] == "monthly_token_budget_exhausted"
    assert ev.data["retry_after_seconds"] == 1234


def test_circuit_tripped_event_schema() -> None:
    ev = circuit_tripped_event(tenant_id="t1", provider="groq", trip_count=2)
    assert ev.event == "circuit.tripped"
    assert ev.data == {"provider": "groq", "trip_count": 2}


def test_audit_chain_broken_event_schema() -> None:
    ev = audit_chain_broken_event(
        tenant_id="t1",
        record_id="r123",
        reason="hash_mismatch",
        ts="2026-05-18T10:00:00",
    )
    assert ev.event == "audit.chain_broken"
    assert ev.data == {
        "record_id": "r123",
        "reason": "hash_mismatch",
        "record_ts": "2026-05-18T10:00:00",
    }


# --------------------------------------------------------------------------- #
# Dispatcher — happy path                                                      #
# --------------------------------------------------------------------------- #


@respx.mock
@pytest.mark.asyncio
async def test_dispatcher_posts_event_with_signed_body() -> None:
    """A configured webhook receives one POST per published event,
    with the X-Pronaos-Signature header matching a hand-computed HMAC
    of the JSON body."""
    url = "https://example.test/webhook"
    secret = "shh"

    route = respx.post(url).mock(return_value=httpx.Response(200))

    dispatcher = WebhookDispatcher()
    try:
        dispatcher.publish(
            WebhookConfig(url=url, secret=secret),
            circuit_tripped_event(tenant_id="t1", provider="groq", trip_count=1),
        )
        # Drain the in-flight delivery before assertions.
        await dispatcher.aclose()
    except Exception:
        await dispatcher.aclose()
        raise

    assert route.call_count == 1
    request = route.calls[0].request

    # Headers as documented.
    assert request.headers["content-type"] == "application/json"
    assert request.headers[EVENT_HEADER] == "circuit.tripped"
    assert request.headers[DELIVERY_HEADER]  # delivery_id present
    # Signature matches a recomputed HMAC over the actual body bytes.
    body = bytes(request.content)
    expected_sig = sign_payload(body, secret)
    assert request.headers[SIGNATURE_HEADER] == expected_sig

    # Body shape: event + ts + tenant_id + data.
    decoded = json.loads(body)
    assert decoded["event"] == "circuit.tripped"
    assert decoded["tenant_id"] == "t1"
    assert decoded["data"]["provider"] == "groq"
    assert "ts" in decoded


@pytest.mark.asyncio
async def test_dispatcher_is_noop_when_unconfigured() -> None:
    """Publishing to a tenant with no webhook configured must NOT
    attempt any HTTP call — should silently no-op. The most-common
    state for production tenants who haven't set up webhooks yet."""
    dispatcher = WebhookDispatcher()
    try:
        # url=None, secret=None: dispatcher should bail without firing.
        dispatcher.publish(
            WebhookConfig(url=None, secret=None),
            circuit_tripped_event(tenant_id="t1", provider="groq", trip_count=1),
        )
        # Pending set should be empty — no task was created.
        assert len(dispatcher._pending) == 0
    finally:
        await dispatcher.aclose()


@pytest.mark.asyncio
async def test_dispatcher_is_noop_when_secret_missing() -> None:
    """Defensive: a partial config (url set but secret None, or vice
    versa) should ALSO be a no-op — sending an unsigned payload would
    be a security smell. Operators must set both or neither."""
    dispatcher = WebhookDispatcher()
    try:
        dispatcher.publish(
            WebhookConfig(url="https://example.test/webhook", secret=None),
            circuit_tripped_event(tenant_id="t1", provider="groq", trip_count=1),
        )
        assert len(dispatcher._pending) == 0
    finally:
        await dispatcher.aclose()


# --------------------------------------------------------------------------- #
# Dispatcher — retry policy                                                    #
# --------------------------------------------------------------------------- #


@respx.mock
@pytest.mark.asyncio
async def test_dispatcher_retries_on_5xx_then_succeeds() -> None:
    """Transient 5xx → retry with exponential backoff. After a successful
    follow-up, no further attempts. ``call_count`` proves the retry
    actually fired."""
    url = "https://example.test/webhook"

    # First call returns 503, second returns 200.
    route = respx.post(url).mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(200),
        ]
    )

    dispatcher = WebhookDispatcher(
        max_attempts=3,
        backoff_base_seconds=0.001,  # near-zero so the test runs fast
    )
    try:
        dispatcher.publish(
            WebhookConfig(url=url, secret="s"),
            circuit_tripped_event(tenant_id="t1", provider="groq", trip_count=1),
        )
        await dispatcher.aclose()
    except Exception:
        await dispatcher.aclose()
        raise

    assert route.call_count == 2


@respx.mock
@pytest.mark.asyncio
async def test_dispatcher_gives_up_after_max_attempts_on_5xx() -> None:
    """Chronic 5xx — give up after ``max_attempts`` total attempts.
    No exception, just a logged terminal failure (webhooks are
    best-effort by design)."""
    url = "https://example.test/webhook"
    route = respx.post(url).mock(return_value=httpx.Response(500))

    dispatcher = WebhookDispatcher(
        max_attempts=3,
        backoff_base_seconds=0.001,
    )
    try:
        dispatcher.publish(
            WebhookConfig(url=url, secret="s"),
            circuit_tripped_event(tenant_id="t1", provider="groq", trip_count=1),
        )
        await dispatcher.aclose()
    except Exception:
        await dispatcher.aclose()
        raise

    # Exactly max_attempts attempts.
    assert route.call_count == 3


@respx.mock
@pytest.mark.asyncio
async def test_dispatcher_does_not_retry_on_4xx() -> None:
    """A 4xx means the receiver said our payload is bad. Retrying with
    the same payload won't help — give up immediately. Saves the
    receiver from logging the same error 3 times."""
    url = "https://example.test/webhook"
    route = respx.post(url).mock(return_value=httpx.Response(400))

    dispatcher = WebhookDispatcher(
        max_attempts=3,
        backoff_base_seconds=0.001,
    )
    try:
        dispatcher.publish(
            WebhookConfig(url=url, secret="s"),
            circuit_tripped_event(tenant_id="t1", provider="groq", trip_count=1),
        )
        await dispatcher.aclose()
    except Exception:
        await dispatcher.aclose()
        raise

    assert route.call_count == 1  # no retries on client error


@respx.mock
@pytest.mark.asyncio
async def test_dispatcher_retries_on_network_error() -> None:
    """Connection refused / DNS failure / TLS handshake error → treat
    same as 5xx. The receiver might be momentarily down."""
    url = "https://example.test/webhook"

    # First raises a connection error; second succeeds.
    route = respx.post(url).mock(
        side_effect=[
            httpx.ConnectError("connection refused"),
            httpx.Response(200),
        ]
    )

    dispatcher = WebhookDispatcher(
        max_attempts=3,
        backoff_base_seconds=0.001,
    )
    try:
        dispatcher.publish(
            WebhookConfig(url=url, secret="s"),
            circuit_tripped_event(tenant_id="t1", provider="groq", trip_count=1),
        )
        await dispatcher.aclose()
    except Exception:
        await dispatcher.aclose()
        raise

    assert route.call_count == 2


# --------------------------------------------------------------------------- #
# Dispatcher — concurrent deliveries                                           #
# --------------------------------------------------------------------------- #


@respx.mock
@pytest.mark.asyncio
async def test_dispatcher_handles_concurrent_events() -> None:
    """Multiple publish() calls in quick succession should each produce
    one delivery — no batching, no dropping. The strong-reference set
    on the dispatcher prevents asyncio.GC from killing tasks
    mid-flight."""
    url = "https://example.test/webhook"
    route = respx.post(url).mock(return_value=httpx.Response(200))

    dispatcher = WebhookDispatcher()
    config = WebhookConfig(url=url, secret="s")
    try:
        for i in range(5):
            dispatcher.publish(
                config,
                circuit_tripped_event(tenant_id="t1", provider="groq", trip_count=i),
            )
        await dispatcher.aclose()
    except Exception:
        await dispatcher.aclose()
        raise

    assert route.call_count == 5


@pytest.mark.asyncio
async def test_dispatcher_aclose_waits_for_pending_deliveries() -> None:
    """aclose() must not return until in-flight deliveries finish — a
    racy shutdown would lose events. We verify by giving the receiver
    a tiny delay; the call_count should still be 1 after aclose()."""
    received = asyncio.Event()
    call_count = 0

    async def _handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        # Tiny delay so the test can race aclose() against in-flight POST.
        await asyncio.sleep(0.05)
        call_count += 1
        received.set()
        return httpx.Response(200)

    url = "https://example.test/webhook"

    with respx.mock(assert_all_called=True) as mock:
        mock.post(url).mock(side_effect=_handler)

        dispatcher = WebhookDispatcher()
        try:
            dispatcher.publish(
                WebhookConfig(url=url, secret="s"),
                circuit_tripped_event(
                    tenant_id="t1", provider="groq", trip_count=1
                ),
            )
            # aclose should wait for the delivery, not race it.
            await dispatcher.aclose()
        except Exception:
            await dispatcher.aclose()
            raise

        assert call_count == 1
        assert received.is_set()
