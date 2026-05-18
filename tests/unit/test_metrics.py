"""Phase-6.1 tests for the Prometheus metrics surface.

Three things to prove:

1. ``GET /metrics`` returns valid Prometheus exposition (correct content-type,
   includes every metric we registered).
2. Counters and histograms move when traffic flows — confirmation that the
   middleware + provider hooks are actually wired, not just imported.
3. Quota denials are recorded with the right ``reason`` label so dashboards
   can split rate-limit denials from budget-exhaustion denials.

Note on isolation: ``pronaos.observability.metrics`` uses a process-wide
registry. We don't reset it between tests — counters are monotonically
increasing within a session, so each test reads the BEFORE value and asserts
on the DELTA. That's correct Prometheus client behaviour and avoids the
fragility of trying to ``unregister`` collectors.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from pronaos.observability import metrics as m
from pronaos.providers.anthropic import ANTHROPIC_API_URL

# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _counter_value(counter: Any, **labels: str) -> float:
    """Read a single counter's current value for the given label set.

    Reaches through prometheus_client's internals so the test doesn't have
    to parse the exposition text. Returns 0.0 when the label combination
    hasn't been observed yet."""
    try:
        return counter.labels(**labels)._value.get()
    except KeyError:
        return 0.0


def _anthropic_response(in_tokens: int = 5, out_tokens: int = 3) -> dict:
    return {
        "id": "msg_01",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": "ok"}],
        "model": "claude-opus-4-7",
        "stop_reason": "end_turn",
        "usage": {"input_tokens": in_tokens, "output_tokens": out_tokens},
    }


# --------------------------------------------------------------------------- #
# /metrics endpoint shape                                                     #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_metrics_endpoint_returns_prometheus_exposition(auth_setup) -> None:  # type: ignore[no-untyped-def]
    """`GET /metrics` must serve the Prometheus text-exposition format with
    the right content-type and at least one of our named metrics in the body —
    proof that the registry is wired to the route, not a default registry."""
    r = await auth_setup.client.get("/metrics")
    assert r.status_code == 200
    assert "text/plain" in r.headers["content-type"]
    body = r.text
    # Every metric we declared should appear in the exposition, even before
    # any traffic — prometheus_client emits HELP/TYPE lines on registration.
    for name in (
        "pronaos_http_requests_total",
        "pronaos_http_request_duration_seconds",
        "pronaos_provider_requests_total",
        "pronaos_provider_tokens_total",
        "pronaos_provider_cost_hcents_total",
        "pronaos_quota_denials_total",
    ):
        assert name in body, f"metric {name} missing from /metrics output"


@pytest.mark.asyncio
async def test_metrics_endpoint_does_not_require_auth(auth_setup) -> None:  # type: ignore[no-untyped-def]
    """Prometheus scrapers don't speak Bearer auth, so /metrics must be
    open inside the network. The data is aggregate counters with no PII —
    deployments restrict scrape access at the network layer, not here."""
    r = await auth_setup.client.get("/metrics")
    assert r.status_code == 200


# --------------------------------------------------------------------------- #
# HTTP middleware records counters/histograms                                 #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_http_counter_increments_on_request(auth_setup) -> None:  # type: ignore[no-untyped-def]
    """A request to /healthz should bump the http_requests_total counter for
    the matched route. The route label MUST be the template path, not the
    raw URL — that's the whole reason cardinality stays bounded."""
    before = _counter_value(
        m.http_requests_total,
        method="GET",
        route="/v1/healthz",
        status_code="200",
    )
    r = await auth_setup.client.get("/v1/healthz")
    assert r.status_code == 200
    after = _counter_value(
        m.http_requests_total,
        method="GET",
        route="/v1/healthz",
        status_code="200",
    )
    assert after - before == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_unmatched_route_uses_unmatched_label(auth_setup) -> None:  # type: ignore[no-untyped-def]
    """A 404 should still be counted but under a low-cardinality fallback
    label rather than echoing the raw URL — otherwise a scanner pounding
    /admin/foo, /admin/bar etc. would balloon the series count."""
    before = _counter_value(
        m.http_requests_total,
        method="GET",
        route="unmatched",
        status_code="404",
    )
    r = await auth_setup.client.get("/this-endpoint-does-not-exist")
    assert r.status_code == 404
    after = _counter_value(
        m.http_requests_total,
        method="GET",
        route="unmatched",
        status_code="404",
    )
    assert after - before == pytest.approx(1.0)


# --------------------------------------------------------------------------- #
# Provider call records counters + tokens + cost                              #
# --------------------------------------------------------------------------- #


@respx.mock
@pytest.mark.asyncio
async def test_successful_chat_records_provider_metrics(auth_setup) -> None:  # type: ignore[no-untyped-def]
    """One chat call → provider_requests_total{status=success}+1,
    provider_tokens_total{direction=prompt}+N, +{completion}+N,
    provider_cost_hcents_total+cost."""
    respx.post(ANTHROPIC_API_URL).mock(
        return_value=httpx.Response(200, json=_anthropic_response(in_tokens=11, out_tokens=7))
    )

    provider_labels = {"provider": "anthropic", "model": "anthropic/claude-opus-4-7"}
    req_before = _counter_value(m.provider_requests_total, **provider_labels, status="success")
    prompt_before = _counter_value(m.provider_tokens_total, **provider_labels, direction="prompt")
    completion_before = _counter_value(
        m.provider_tokens_total, **provider_labels, direction="completion"
    )
    cost_before = _counter_value(m.provider_cost_hcents_total, **provider_labels)

    resp = await auth_setup.client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {auth_setup.api_key}"},
        json={
            "model": "anthropic/claude-opus-4-7",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert resp.status_code == 200, resp.text

    assert _counter_value(m.provider_requests_total, **provider_labels, status="success") - req_before == pytest.approx(1.0)
    assert _counter_value(m.provider_tokens_total, **provider_labels, direction="prompt") - prompt_before == pytest.approx(11.0)
    assert _counter_value(m.provider_tokens_total, **provider_labels, direction="completion") - completion_before == pytest.approx(7.0)
    # Don't pin the exact cost (pricing map can change); just confirm the
    # cost counter MOVED — an Opus call with 18 tokens should never be free.
    assert _counter_value(m.provider_cost_hcents_total, **provider_labels) > cost_before
