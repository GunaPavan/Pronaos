"""Phase-6.3 tests: custom OTEL spans for quota + provider.

These tests run with a synchronous TracerProvider + InMemorySpanExporter so
we can assert directly on emitted spans without spinning up a collector.

The tracer-provider swap is tricky: OpenTelemetry's
``trace.set_tracer_provider`` writes to a one-shot global. We install our
test provider exactly once per session and re-use it across tests, just
clearing the exporter between assertions. ``configure_tracing()`` is a
no-op in tests because ``otel_enabled`` is False, so there's no fight over
the global.
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
import respx
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from pronaos.providers.anthropic import ANTHROPIC_API_URL

# --------------------------------------------------------------------------- #
# Tracer fixture                                                              #
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="session")
def _otel_exporter() -> InMemorySpanExporter:
    """Install a real TracerProvider once per session.

    ``trace.set_tracer_provider`` is a one-shot global — other test
    modules may have installed their own provider before this fixture
    runs. So instead of fighting for ownership, we ADD our exporter
    as another span processor to whichever provider is live. If no
    real provider exists yet, install one.

    Either way, every span produced by the gateway during this test
    session lands in our exporter."""
    exporter = InMemorySpanExporter()
    current = trace.get_tracer_provider()
    if isinstance(current, TracerProvider):
        current.add_span_processor(SimpleSpanProcessor(exporter))
    else:
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
    return exporter


@pytest.fixture
def spans(_otel_exporter: InMemorySpanExporter) -> Iterator[InMemorySpanExporter]:
    """Per-test handle: yields the exporter, clears it on entry, lets the
    test inspect ``.get_finished_spans()`` after the body runs."""
    _otel_exporter.clear()
    yield _otel_exporter


# --------------------------------------------------------------------------- #
# Quota check span                                                            #
# --------------------------------------------------------------------------- #


@respx.mock
@pytest.mark.asyncio
async def test_quota_check_emits_span_with_allowed_attribute(
    auth_setup,  # type: ignore[no-untyped-def]
    spans: InMemorySpanExporter,
) -> None:
    """A successful request must produce a ``pronaos.quota.check`` span with
    ``pronaos.quota.allowed=True``. This is the span an SRE will pivot on
    when answering 'was this request gated by the quota layer?'"""
    respx.post(ANTHROPIC_API_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "msg_01",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": "ok"}],
                "model": "claude-opus-4-7",
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )
    )
    r = await auth_setup.client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {auth_setup.api_key}"},
        json={
            "model": "anthropic/claude-opus-4-7",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert r.status_code == 200, r.text

    finished = spans.get_finished_spans()
    quota_spans = [s for s in finished if s.name == "pronaos.quota.check"]
    assert quota_spans, (
        f"expected a pronaos.quota.check span; got names={[s.name for s in finished]}"
    )
    assert quota_spans[0].attributes["pronaos.quota.allowed"] is True


# --------------------------------------------------------------------------- #
# Provider call span                                                          #
# --------------------------------------------------------------------------- #


@respx.mock
@pytest.mark.asyncio
async def test_provider_call_span_carries_tokens_and_cost(
    auth_setup,  # type: ignore[no-untyped-def]
    spans: InMemorySpanExporter,
) -> None:
    """The provider-call span must carry provider, model, tokens, and cost
    as attributes — that's the FinOps pivot story when inspecting a trace
    for an expensive request.

    Phase 43 renamed the span to follow the OTel GenAI spec
    (``chat {model}``); the pronaos.* attributes stay alongside the new
    gen_ai.* ones for backward compatibility with existing dashboards.
    """
    respx.post(ANTHROPIC_API_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "msg_01",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": "ok"}],
                "model": "claude-opus-4-7",
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 17, "output_tokens": 9},
            },
        )
    )
    r = await auth_setup.client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {auth_setup.api_key}"},
        json={
            "model": "anthropic/claude-opus-4-7",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert r.status_code == 200, r.text

    finished = spans.get_finished_spans()
    # Find the span that has pronaos.provider set — that's our chat-call
    # span regardless of whether it follows the old or new name convention.
    provider_spans = [
        s
        for s in finished
        if s.attributes and dict(s.attributes).get("pronaos.provider") == "anthropic"
    ]
    assert provider_spans, "no provider-call span emitted"
    span = provider_spans[0]
    # New name follows the OTel GenAI convention: "chat {model}".
    assert span.name == "chat anthropic/claude-opus-4-7"
    attrs = dict(span.attributes or {})
    # Pronaos-custom attributes (back-compat for existing Grafana panels).
    assert attrs["pronaos.provider"] == "anthropic"
    assert attrs["pronaos.model"] == "anthropic/claude-opus-4-7"
    assert attrs["pronaos.prompt_tokens"] == 17
    assert attrs["pronaos.completion_tokens"] == 9
    # Cost depends on the pricing map (which can change) — assert that it
    # was set to a positive number, not that it equals any specific value.
    assert isinstance(attrs["pronaos.cost_hcents"], int)
    assert attrs["pronaos.cost_hcents"] > 0
    # Phase 43 — spec-compliant gen_ai.* attributes are ALSO present.
    assert attrs["gen_ai.operation.name"] == "chat"
    assert attrs["gen_ai.system"] == "anthropic"
    assert attrs["gen_ai.request.model"] == "anthropic/claude-opus-4-7"
    assert attrs["gen_ai.usage.input_tokens"] == 17
    assert attrs["gen_ai.usage.output_tokens"] == 9


# NOTE: A test for the ``no_response`` error attribute would be valuable
# but the Anthropic adapter still emits a usage chunk for an empty
# content[] response, so triggering the chunk-is-None branch via respx
# mocking is fiddly. The attribute is set in chat.py's no-chunk branch and
# the metric-counter test in test_metrics.py covers the equivalent error
# path. Worth revisiting if/when we add a "force-zero-chunks" hook in the
# adapter test surface.
