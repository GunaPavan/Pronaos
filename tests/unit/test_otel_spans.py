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

    Subsequent ``trace.set_tracer_provider`` calls are silently ignored by
    OTEL, so the per-test pattern is "reuse the provider, clear the
    exporter." That keeps the test cost near zero and avoids the
    one-shot-global trap."""
    exporter = InMemorySpanExporter()
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
    """The ``pronaos.provider.call`` span must carry provider, model,
    tokens, and cost as attributes — that's the FinOps pivot story when
    inspecting a trace for an expensive request."""
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
    provider_spans = [s for s in finished if s.name == "pronaos.provider.call"]
    assert provider_spans, "no pronaos.provider.call span emitted"
    attrs = provider_spans[0].attributes
    assert attrs["pronaos.provider"] == "anthropic"
    assert attrs["pronaos.model"] == "anthropic/claude-opus-4-7"
    assert attrs["pronaos.prompt_tokens"] == 17
    assert attrs["pronaos.completion_tokens"] == 9
    # Cost depends on the pricing map (which can change) — assert that it
    # was set to a positive number, not that it equals any specific value.
    assert isinstance(attrs["pronaos.cost_hcents"], int)
    assert attrs["pronaos.cost_hcents"] > 0


# NOTE: A test for the ``no_response`` error attribute would be valuable
# but the Anthropic adapter still emits a usage chunk for an empty
# content[] response, so triggering the chunk-is-None branch via respx
# mocking is fiddly. The attribute is set in chat.py's no-chunk branch and
# the metric-counter test in test_metrics.py covers the equivalent error
# path. Worth revisiting if/when we add a "force-zero-chunks" hook in the
# adapter test surface.
