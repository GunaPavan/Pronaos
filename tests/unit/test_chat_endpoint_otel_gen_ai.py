"""End-to-end OTel GenAI test (Phase 43).

Captures the span(s) produced by a real chat call through the FastAPI
stack and asserts the OTel GenAI semantic conventions are followed
end-to-end:

- Span name follows the ``{operation} {model}`` convention.
- ``gen_ai.system`` is the spec-vocabulary value (e.g. ``groq``).
- ``gen_ai.request.model`` matches the requested model.
- ``gen_ai.usage.input_tokens`` + ``gen_ai.usage.output_tokens`` are
  ints (the spec is strict on integer types).
- ``gen_ai.response.finish_reasons`` is an array, never a scalar.
- The old ``pronaos.*`` attributes are still set alongside (backward
  compatibility with existing Grafana panels).

We use OTel's ``InMemorySpanExporter`` — that's the *real* OTel SDK
exporter, not a mock. The span-attribute serialisation, type coercion,
and processor pipeline all run.
"""

from __future__ import annotations

import httpx
import pytest
import respx
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from pronaos.observability.otel_gen_ai import (
    recommended_response_attributes,
    required_request_attributes,
)

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"


def _groq_response() -> dict[str, object]:
    return {
        "id": "chatcmpl-test123",
        "object": "chat.completion",
        "model": "llama-3.1-8b-instant",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "OK."},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 9, "completion_tokens": 2, "total_tokens": 11},
    }


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


_EXPORTER: InMemorySpanExporter | None = None


def _ensure_exporter() -> InMemorySpanExporter:
    """Attach an in-memory exporter to the global TracerProvider.

    OTel's ``set_tracer_provider`` is a one-shot — once set, further
    calls are ignored with a warning. Other tests (notably
    ``test_otel_spans.py``) may have installed their own provider
    earlier in the session. So instead of fighting for ownership, we
    ADD our exporter as an additional span processor to whichever
    provider is live. If no real provider exists yet, install one.

    Result: every span produced by the gateway lands in BOTH our
    exporter and whatever the other tests installed — fan-out is
    cheap and Pythonic.
    """
    global _EXPORTER
    if _EXPORTER is not None:
        return _EXPORTER

    exporter = InMemorySpanExporter()
    current = trace.get_tracer_provider()
    if isinstance(current, TracerProvider):
        # Real provider already installed by another test fixture; just
        # attach our exporter alongside theirs.
        current.add_span_processor(SimpleSpanProcessor(exporter))
    else:
        # No real provider yet (this test runs first). Install ours.
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
    _EXPORTER = exporter
    return exporter


@pytest.fixture
def gen_ai_spans() -> InMemorySpanExporter:
    """Hand the shared exporter to the test, after clearing it."""
    exporter = _ensure_exporter()
    exporter.clear()
    return exporter


@respx.mock
@pytest.mark.asyncio
async def test_chat_emits_spec_compliant_gen_ai_span(
    auth_setup,  # type: ignore[no-untyped-def]
    gen_ai_spans,  # type: ignore[no-untyped-def]
) -> None:
    respx.post(GROQ_URL).mock(return_value=httpx.Response(200, json=_groq_response()))

    r = await auth_setup.client.post(
        "/v1/chat/completions",
        headers=_auth(auth_setup.api_key),
        json={
            "model": "groq/llama-3.1-8b-instant",
            "messages": [{"role": "user", "content": "say OK"}],
            "max_tokens": 10,
            "temperature": 0.0,
        },
    )
    assert r.status_code == 200, r.text

    # Pull the provider-call span out of the in-memory exporter.
    spans = gen_ai_spans.get_finished_spans()
    provider_spans = [
        s for s in spans if s.attributes and "gen_ai.operation.name" in dict(s.attributes)
    ]
    assert provider_spans, f"no gen_ai span captured; got {[s.name for s in spans]}"
    span = provider_spans[0]
    attrs = dict(span.attributes or {})

    # ---- Span name follows the OTel GenAI convention ---------------------
    assert span.name == "chat groq/llama-3.1-8b-instant"

    # ---- Required request attributes -------------------------------------
    for req in required_request_attributes():
        assert req in attrs, f"required attr {req!r} missing; got {attrs}"
    assert attrs["gen_ai.operation.name"] == "chat"
    assert attrs["gen_ai.system"] == "groq"
    assert attrs["gen_ai.request.model"] == "groq/llama-3.1-8b-instant"

    # ---- Recommended optional request attributes (set when provided) -----
    assert attrs["gen_ai.request.max_tokens"] == 10
    assert attrs["gen_ai.request.temperature"] == pytest.approx(0.0)

    # ---- Recommended response attributes ---------------------------------
    for rec in recommended_response_attributes():
        assert rec in attrs, f"recommended attr {rec!r} missing; got {attrs}"
    assert attrs["gen_ai.usage.input_tokens"] == 9
    assert attrs["gen_ai.usage.output_tokens"] == 2
    # finish_reasons is plural; OTel serialises tuples as arrays. Reading
    # back gives a tuple or a list — both have `tuple(...)` semantics.
    assert tuple(attrs["gen_ai.response.finish_reasons"]) == ("stop",)
    # Response ID + model from the upstream response (Phase 43 experimental
    # attributes).
    assert attrs["gen_ai.response.id"] == "chatcmpl-test123"
    assert attrs["gen_ai.response.model"] == "llama-3.1-8b-instant"

    # ---- Backward compatibility — pronaos.* attrs still present ----------
    # Existing Grafana panels filter on pronaos.provider + pronaos.model;
    # we keep them stamped so dashboards don't break.
    assert attrs["pronaos.provider"] == "groq"
    assert attrs["pronaos.model"] == "groq/llama-3.1-8b-instant"
    assert attrs["pronaos.prompt_tokens"] == 9
    assert attrs["pronaos.completion_tokens"] == 2


@respx.mock
@pytest.mark.asyncio
async def test_no_temperature_attr_when_request_omits_it(
    auth_setup,  # type: ignore[no-untyped-def]
    gen_ai_spans,  # type: ignore[no-untyped-def]
) -> None:
    """If the client doesn't send a temperature, the gen_ai.request.temperature
    attribute should NOT appear (per spec — only set when supplied).
    Receivers filtering on presence shouldn't see phantom defaults."""
    respx.post(GROQ_URL).mock(return_value=httpx.Response(200, json=_groq_response()))

    r = await auth_setup.client.post(
        "/v1/chat/completions",
        headers=_auth(auth_setup.api_key),
        json={
            "model": "groq/llama-3.1-8b-instant",
            "messages": [{"role": "user", "content": "hi"}],
            # No max_tokens, no temperature.
        },
    )
    assert r.status_code == 200, r.text

    spans = gen_ai_spans.get_finished_spans()
    provider_spans = [
        s for s in spans if s.attributes and "gen_ai.operation.name" in dict(s.attributes)
    ]
    attrs = dict(provider_spans[0].attributes or {})
    assert "gen_ai.request.temperature" not in attrs
    assert "gen_ai.request.max_tokens" not in attrs
    # But required attrs ARE still set.
    assert attrs["gen_ai.system"] == "groq"
    assert attrs["gen_ai.request.model"] == "groq/llama-3.1-8b-instant"
