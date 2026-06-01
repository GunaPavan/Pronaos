"""OpenTelemetry GenAI semantic conventions (Phase 43).

The OTel GenAI semantic conventions standardise span shapes for
LLM-gateway-like systems so observability backends (Datadog, Honeycomb,
Splunk, Grafana Tempo) can ship GenAI-specific dashboards that key off
the same attribute names everyone uses.

Spec source: https://opentelemetry.io/docs/specs/semconv/gen-ai/

What this module does
---------------------
- Maps Pronaos's internal provider keys (``groq``, ``bedrock``, etc.)
  to the spec's ``gen_ai.system`` vocabulary (e.g. ``aws.bedrock``).
- Provides ``apply_gen_ai_request_attrs(span, ...)`` and
  ``apply_gen_ai_response_attrs(span, ...)`` — two calls per span,
  one before the upstream call and one after, that stamp every
  required + recommended attribute.
- Recommends the spec-compliant span name (``chat {model}``,
  ``embeddings {model}``, ``rerank {model}``) so dashboards' span-name
  filters work out of the box.

What this module does NOT do
----------------------------
- Replace the existing ``pronaos.*`` attributes. They stay alongside
  the new ``gen_ai.*`` attributes for backward compatibility with
  existing dashboards and Grafana panels.
- Emit ``gen_ai.user.message`` / ``gen_ai.assistant.message`` events
  for individual message bodies. The spec marks these optional and
  notes PII concerns; we keep prompts out of traces by default.
  Operators who want them can wire it via the chat handler.

The spec is actively evolving (Stable for some attributes, Experimental
for others as of May 2026). This module covers the **Stable** subset
plus a small number of high-utility Experimental attributes
(``gen_ai.response.id``, ``gen_ai.response.model``).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Final

from opentelemetry.trace import Span

# --------------------------------------------------------------------------- #
# gen_ai.system vocabulary                                                    #
# --------------------------------------------------------------------------- #
#
# The spec defines a controlled vocabulary for ``gen_ai.system``. Where
# Pronaos has a provider key that's in the vocabulary we map directly;
# where it isn't (because the spec doesn't list every OSS provider) we
# use a sensible custom string. Custom values are explicitly permitted
# by the spec — receivers should treat unknown values as opaque labels.
#
# Spec vocabulary entries (as of May 2026):
#   anthropic, aws.bedrock, azure.ai.inference, azure.ai.openai,
#   cohere, deepseek, gcp.gemini, gcp.gen_ai, gcp.vertex_ai, groq,
#   ibm.watsonx.ai, mistral_ai, openai, perplexity, xai
#
# Everything not in the spec uses a Pronaos-defined value (lowercase,
# underscore-separated where multi-word). This keeps observability
# backends from displaying a confusing mix of "groq" and "Groq".

_GEN_AI_SYSTEM_BY_PROVIDER: Final[dict[str, str]] = {
    "anthropic": "anthropic",
    "bedrock": "aws.bedrock",
    "openai": "openai",
    "azure_openai": "azure.ai.openai",
    "groq": "groq",
    "mistral": "mistral_ai",
    "deepseek": "deepseek",
    "xai": "xai",
    "perplexity": "perplexity",
    "cohere": "cohere",
    # Not in the spec vocabulary; custom values.
    "voyage": "voyage",
    "together": "together_ai",
    "fireworks": "fireworks_ai",
    "cerebras": "cerebras",
    "openrouter": "openrouter",
    "ollama": "ollama",
}


def gen_ai_system_for(provider_key: str) -> str:
    """Map a Pronaos provider key to the ``gen_ai.system`` value.

    Returns the provider key unchanged if there's no mapping — that
    way new providers don't crash the trace path before someone
    registers them here.
    """
    return _GEN_AI_SYSTEM_BY_PROVIDER.get(provider_key, provider_key)


# --------------------------------------------------------------------------- #
# Operation names                                                             #
# --------------------------------------------------------------------------- #
#
# The spec defines a small set of operation names. Pronaos's three
# inference endpoints map cleanly:

GEN_AI_OPERATION_CHAT: Final = "chat"
GEN_AI_OPERATION_EMBEDDINGS: Final = "embeddings"
GEN_AI_OPERATION_RERANK: Final = "rerank"


def span_name_for(operation: str, model: str) -> str:
    """Build the spec-compliant span name: ``{operation} {model}``.

    Examples:
        ``chat gpt-4o``
        ``chat anthropic.claude-3-5-haiku-20241022-v1:0``
        ``embeddings text-embedding-3-small``

    The spec recommends "low cardinality" span names; since model
    names are bounded per-provider (we have ~30 across the catalog)
    this stays well below the per-service span-name budget any
    backend imposes.
    """
    return f"{operation} {model}"


# --------------------------------------------------------------------------- #
# Request attributes                                                          #
# --------------------------------------------------------------------------- #


def apply_gen_ai_request_attrs(
    span: Span,
    *,
    operation: str,
    system: str,
    request_model: str,
    max_tokens: int | None = None,
    temperature: float | None = None,
    top_p: float | None = None,
    stop_sequences: Sequence[str] | None = None,
) -> None:
    """Set the request-side ``gen_ai.*`` attributes on a span.

    Required attributes per spec:
      - ``gen_ai.operation.name``
      - ``gen_ai.system``
      - ``gen_ai.request.model``

    Recommended attributes (set only when present in the request):
      - ``gen_ai.request.max_tokens``
      - ``gen_ai.request.temperature``
      - ``gen_ai.request.top_p``
      - ``gen_ai.request.stop_sequences``
    """
    span.set_attribute("gen_ai.operation.name", operation)
    span.set_attribute("gen_ai.system", system)
    span.set_attribute("gen_ai.request.model", request_model)
    if max_tokens is not None:
        span.set_attribute("gen_ai.request.max_tokens", int(max_tokens))
    if temperature is not None:
        span.set_attribute("gen_ai.request.temperature", float(temperature))
    if top_p is not None:
        span.set_attribute("gen_ai.request.top_p", float(top_p))
    if stop_sequences:
        # Spec requires an array; coerce to a tuple of strings so the
        # exporter doesn't try to set a single comma-joined string.
        span.set_attribute(
            "gen_ai.request.stop_sequences",
            tuple(str(s) for s in stop_sequences),
        )


# --------------------------------------------------------------------------- #
# Response attributes                                                         #
# --------------------------------------------------------------------------- #


def apply_gen_ai_response_attrs(
    span: Span,
    *,
    response_model: str | None = None,
    response_id: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    finish_reasons: Sequence[str] | None = None,
) -> None:
    """Set the response-side ``gen_ai.*`` attributes on a span.

    Stable attributes:
      - ``gen_ai.usage.input_tokens``
      - ``gen_ai.usage.output_tokens``
      - ``gen_ai.response.finish_reasons`` (ARRAY — note the plural)

    Experimental but high-utility attributes:
      - ``gen_ai.response.id``
      - ``gen_ai.response.model``

    The plural ``finish_reasons`` is the spec's choice — a single
    chat completion can have multiple choices, each with its own
    finish reason. For non-multi-choice gateways (Pronaos today),
    pass a one-element array.
    """
    if response_model is not None:
        span.set_attribute("gen_ai.response.model", response_model)
    if response_id is not None:
        span.set_attribute("gen_ai.response.id", response_id)
    if input_tokens is not None:
        span.set_attribute("gen_ai.usage.input_tokens", int(input_tokens))
    if output_tokens is not None:
        span.set_attribute("gen_ai.usage.output_tokens", int(output_tokens))
    if finish_reasons:
        span.set_attribute(
            "gen_ai.response.finish_reasons",
            tuple(str(r) for r in finish_reasons),
        )


# --------------------------------------------------------------------------- #
# Compliance checklist                                                        #
# --------------------------------------------------------------------------- #


def required_request_attributes() -> tuple[str, ...]:
    """Attributes the spec marks REQUIRED on every GenAI span.

    Tests assert these are always present after
    :func:`apply_gen_ai_request_attrs`.
    """
    return (
        "gen_ai.operation.name",
        "gen_ai.system",
        "gen_ai.request.model",
    )


def recommended_response_attributes() -> tuple[str, ...]:
    """Attributes the spec marks recommended/stable on the response side.

    Tests assert these are present when the upstream returned usage
    data (almost always for chat; never for embeddings since
    embeddings have no finish_reason).
    """
    return (
        "gen_ai.usage.input_tokens",
        "gen_ai.usage.output_tokens",
        "gen_ai.response.finish_reasons",
    )


def all_gen_ai_attributes(span_attributes: dict[str, Any]) -> dict[str, Any]:
    """Filter a span attribute dict down to the ``gen_ai.*`` namespace.

    Test helper — lets the integration test grep for the spec
    attributes without picking up the Pronaos-custom ones.
    """
    return {k: v for k, v in span_attributes.items() if k.startswith("gen_ai.")}
