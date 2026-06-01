"""Unit tests for the OTel GenAI semantic-conventions helpers (Phase 43).

Three surfaces under test:

1. ``gen_ai_system_for`` — provider-key → spec-vocabulary mapping.
2. ``apply_gen_ai_request_attrs`` — sets the right request attributes
   on a span, with the right types, only for non-None fields.
3. ``apply_gen_ai_response_attrs`` — same shape for the response side,
   with the special-case array typing for ``finish_reasons``.

We use OTel's ``InMemorySpanExporter`` so the spans we set attributes
on are real ``ReadableSpan`` objects — we're not testing against
a mock, we're testing against the actual OTel SDK code paths.
"""

from __future__ import annotations

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from pronaos.observability.otel_gen_ai import (
    GEN_AI_OPERATION_CHAT,
    GEN_AI_OPERATION_EMBEDDINGS,
    GEN_AI_OPERATION_RERANK,
    all_gen_ai_attributes,
    apply_gen_ai_request_attrs,
    apply_gen_ai_response_attrs,
    gen_ai_system_for,
    recommended_response_attributes,
    required_request_attributes,
    span_name_for,
)


@pytest.fixture
def exporter() -> InMemorySpanExporter:
    """Spin up an isolated in-memory tracer for this test.

    Each test gets a fresh TracerProvider so concurrent tests don't
    see each other's spans.
    """
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    # Stash on a fixture-scoped object so the test can pull the
    # tracer matching THIS provider, not the global one.
    exporter._provider = provider  # type: ignore[attr-defined]
    return exporter


def _make_span(exporter: InMemorySpanExporter, name: str) -> tuple[Any, Any]:  # type: ignore[name-defined]
    """Helper: open a span on the fixture's provider, return (cm, span)."""
    tracer = exporter._provider.get_tracer("test")  # type: ignore[attr-defined]
    cm = tracer.start_as_current_span(name)
    return cm, cm


# --------------------------------------------------------------------------- #
# gen_ai_system_for                                                           #
# --------------------------------------------------------------------------- #


class TestGenAiSystemFor:
    def test_known_spec_vocabulary(self) -> None:
        assert gen_ai_system_for("openai") == "openai"
        assert gen_ai_system_for("anthropic") == "anthropic"
        assert gen_ai_system_for("groq") == "groq"
        assert gen_ai_system_for("cohere") == "cohere"

    def test_aws_bedrock_special_case(self) -> None:
        """``bedrock`` → ``aws.bedrock`` per spec vocabulary."""
        assert gen_ai_system_for("bedrock") == "aws.bedrock"

    def test_mistral_special_case(self) -> None:
        """Spec uses ``mistral_ai`` (with underscore + AI suffix)."""
        assert gen_ai_system_for("mistral") == "mistral_ai"

    def test_unknown_provider_passes_through(self) -> None:
        """Unknown keys aren't in the spec; we pass them through so a
        new provider doesn't break the trace path."""
        assert gen_ai_system_for("brand-new-provider") == "brand-new-provider"


# --------------------------------------------------------------------------- #
# span_name_for                                                               #
# --------------------------------------------------------------------------- #


class TestSpanNameFor:
    def test_chat_format(self) -> None:
        assert span_name_for(GEN_AI_OPERATION_CHAT, "gpt-4o") == "chat gpt-4o"

    def test_embeddings_format(self) -> None:
        assert (
            span_name_for(GEN_AI_OPERATION_EMBEDDINGS, "text-embedding-3-small")
            == "embeddings text-embedding-3-small"
        )

    def test_rerank_format(self) -> None:
        assert (
            span_name_for(GEN_AI_OPERATION_RERANK, "rerank-english-v3.0")
            == "rerank rerank-english-v3.0"
        )

    def test_bedrock_model_name_with_dots_and_version_preserved(self) -> None:
        """Bedrock model IDs contain dots + colons; the spec doesn't
        forbid them in span names. Make sure we don't sanitise."""
        out = span_name_for(GEN_AI_OPERATION_CHAT, "anthropic.claude-3-5-haiku-20241022-v1:0")
        assert out == "chat anthropic.claude-3-5-haiku-20241022-v1:0"


# --------------------------------------------------------------------------- #
# apply_gen_ai_request_attrs                                                  #
# --------------------------------------------------------------------------- #


class TestApplyGenAiRequestAttrs:
    def test_required_attrs_always_set(self, exporter: InMemorySpanExporter) -> None:
        tracer = exporter._provider.get_tracer("test")  # type: ignore[attr-defined]
        with tracer.start_as_current_span("chat gpt-4o") as span:
            apply_gen_ai_request_attrs(
                span,
                operation=GEN_AI_OPERATION_CHAT,
                system="openai",
                request_model="gpt-4o",
            )
        spans = exporter.get_finished_spans()
        attrs = dict(spans[0].attributes or {})
        for req in required_request_attributes():
            assert req in attrs, f"missing required attr {req!r}; got {attrs}"
        assert attrs["gen_ai.operation.name"] == "chat"
        assert attrs["gen_ai.system"] == "openai"
        assert attrs["gen_ai.request.model"] == "gpt-4o"

    def test_optional_attrs_set_when_present(self, exporter: InMemorySpanExporter) -> None:
        tracer = exporter._provider.get_tracer("test")  # type: ignore[attr-defined]
        with tracer.start_as_current_span("chat gpt-4o") as span:
            apply_gen_ai_request_attrs(
                span,
                operation=GEN_AI_OPERATION_CHAT,
                system="openai",
                request_model="gpt-4o",
                max_tokens=100,
                temperature=0.7,
                top_p=0.95,
                stop_sequences=["\n\n", "END"],
            )
        attrs = dict(exporter.get_finished_spans()[0].attributes or {})
        assert attrs["gen_ai.request.max_tokens"] == 100
        assert attrs["gen_ai.request.temperature"] == pytest.approx(0.7)
        assert attrs["gen_ai.request.top_p"] == pytest.approx(0.95)
        # The spec REQUIRES finish_reasons to be an array; ditto
        # stop_sequences. OTel exporters serialise tuples as arrays.
        assert tuple(attrs["gen_ai.request.stop_sequences"]) == ("\n\n", "END")

    def test_optional_attrs_omitted_when_none(self, exporter: InMemorySpanExporter) -> None:
        tracer = exporter._provider.get_tracer("test")  # type: ignore[attr-defined]
        with tracer.start_as_current_span("chat gpt-4o") as span:
            apply_gen_ai_request_attrs(
                span,
                operation=GEN_AI_OPERATION_CHAT,
                system="openai",
                request_model="gpt-4o",
            )
        attrs = dict(exporter.get_finished_spans()[0].attributes or {})
        # None-valued attributes are NOT set on the span. Receivers
        # that filter on presence (e.g. Datadog dashboards) don't
        # get phantom null fields.
        assert "gen_ai.request.max_tokens" not in attrs
        assert "gen_ai.request.temperature" not in attrs
        assert "gen_ai.request.top_p" not in attrs
        assert "gen_ai.request.stop_sequences" not in attrs

    def test_temperature_coerced_to_float(self, exporter: InMemorySpanExporter) -> None:
        """OTel rejects mixed int/float in numeric attributes for
        some exporters; we coerce to float to avoid the trap."""
        tracer = exporter._provider.get_tracer("test")  # type: ignore[attr-defined]
        with tracer.start_as_current_span("chat") as span:
            apply_gen_ai_request_attrs(
                span,
                operation="chat",
                system="openai",
                request_model="gpt-4o",
                temperature=1,  # int, not float
            )
        attrs = dict(exporter.get_finished_spans()[0].attributes or {})
        assert isinstance(attrs["gen_ai.request.temperature"], float)


# --------------------------------------------------------------------------- #
# apply_gen_ai_response_attrs                                                 #
# --------------------------------------------------------------------------- #


class TestApplyGenAiResponseAttrs:
    def test_full_response(self, exporter: InMemorySpanExporter) -> None:
        tracer = exporter._provider.get_tracer("test")  # type: ignore[attr-defined]
        with tracer.start_as_current_span("chat gpt-4o") as span:
            apply_gen_ai_response_attrs(
                span,
                response_model="gpt-4o-2024-08-06",
                response_id="chatcmpl-abc123",
                input_tokens=20,
                output_tokens=5,
                finish_reasons=["stop"],
            )
        attrs = dict(exporter.get_finished_spans()[0].attributes or {})
        for req in recommended_response_attributes():
            assert req in attrs
        assert attrs["gen_ai.usage.input_tokens"] == 20
        assert attrs["gen_ai.usage.output_tokens"] == 5
        # ``finish_reasons`` is PLURAL — must be an array even with one element.
        assert tuple(attrs["gen_ai.response.finish_reasons"]) == ("stop",)
        assert attrs["gen_ai.response.id"] == "chatcmpl-abc123"
        assert attrs["gen_ai.response.model"] == "gpt-4o-2024-08-06"

    def test_finish_reasons_array_even_single_choice(self, exporter: InMemorySpanExporter) -> None:
        """A single-choice completion still gets a one-element array
        for finish_reasons; the spec is non-negotiable about this."""
        tracer = exporter._provider.get_tracer("test")  # type: ignore[attr-defined]
        with tracer.start_as_current_span("chat") as span:
            apply_gen_ai_response_attrs(span, finish_reasons=("stop",))
        attrs = dict(exporter.get_finished_spans()[0].attributes or {})
        value = attrs["gen_ai.response.finish_reasons"]
        # OTel serialises tuples/lists as arrays; reading back gives a
        # tuple. Check it's iterable and contains the right value.
        assert tuple(value) == ("stop",)

    def test_none_fields_skipped(self, exporter: InMemorySpanExporter) -> None:
        tracer = exporter._provider.get_tracer("test")  # type: ignore[attr-defined]
        with tracer.start_as_current_span("chat") as span:
            apply_gen_ai_response_attrs(span)
        attrs = dict(exporter.get_finished_spans()[0].attributes or {})
        # All fields None -> no gen_ai.response.* / gen_ai.usage.* attrs.
        for required in recommended_response_attributes():
            assert required not in attrs


# --------------------------------------------------------------------------- #
# all_gen_ai_attributes filter                                                #
# --------------------------------------------------------------------------- #


class TestAllGenAiAttributes:
    def test_filters_to_gen_ai_namespace(self) -> None:
        mixed = {
            "gen_ai.system": "openai",
            "gen_ai.request.model": "gpt-4o",
            "pronaos.provider": "openai",
            "pronaos.model": "gpt-4o",
            "service.name": "pronaos",
        }
        filtered = all_gen_ai_attributes(mixed)
        assert set(filtered.keys()) == {"gen_ai.system", "gen_ai.request.model"}


# Needed by _make_span helper imports above.
from typing import Any  # noqa: E402
