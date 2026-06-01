"""Structured-output validation + auto-retry (Phase 39).

LLMs are unreliable on structured output. Even with provider-native
mechanisms (OpenAI ``response_format``, Anthropic JSON-mode-via-tool),
schema violations happen at non-trivial rates: optional fields treated
as required, wrong types (string for number, string for array), small
hallucinated extra fields, occasionally raw markdown leaking around the
JSON object. Every team building on top of an LLM API writes the same
boilerplate to handle this — parse, validate, retry with feedback,
give up.

This module ships that loop ONCE at the gateway. A client supplies a
JSON Schema; the gateway:

1. Forwards the schema to the upstream provider via its native
   mechanism when ``provider_native=True`` AND the provider supports
   it (catalog capability check).
2. Otherwise falls back to schema-guided prompting — injects a system
   message instructing the LLM to respond with JSON matching the
   schema.
3. After the upstream response, **validates** the assistant's content
   against the schema using ``jsonschema``.
4. On violation, builds a **corrective user message** describing the
   exact errors and re-fires the completion (up to ``max_retries``).
5. Returns the first valid response. If all retries fail, returns the
   last response with header ``X-Pronaos-Schema-Validation: failed``
   and a metric increment so operators see the persistent failure
   rate per model.

The corrective message format is deterministic and concise:

    Your previous response failed JSON Schema validation:
    - <path>: <error>
    - <path>: <error>
    Please respond again with valid JSON matching the schema.

We feed BOTH the failed assistant response AND the corrective user
turn into the next request — the model has the failure in context
and self-corrects. This is the standard "constitutional" pattern.

Cost note: each retry is a real upstream call, billed normally as a
separate ``usage_records`` row. Operators reading dashboards see the
overhead as a ramp in the call count for the affected team. The
``X-Pronaos-Schema-Retry-Count`` response header lets clients
attribute the latency for any given call.

Cache interaction: cache hits skip validation entirely — the
cached response was already validated when it was first written.
Cache writes only happen on the *final* valid response, never on
intermediate failures.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Final

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

# Max errors to include in the corrective prompt. Beyond ~10 the LLM
# starts ignoring the trailing entries and the corrective signal
# degrades. Empirically 10 covers nearly every real-world violation
# (most failures have <3 errors).
_MAX_ERRORS_IN_PROMPT: Final = 10

# Pattern to find a JSON object embedded in markdown code fences. Some
# models default to ```json {...} ``` even when asked for raw JSON.
# We strip the fence before validation so a syntactically valid JSON
# body still validates.
_FENCED_JSON_RE: Final = re.compile(
    r"```(?:json)?\s*\n?(.*?)\n?```", re.DOTALL | re.IGNORECASE
)


# --------------------------------------------------------------------------- #
# Result types                                                                #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """Outcome of one validation attempt.

    ``passed`` is the only field callers branch on. ``errors`` carries
    the formatted error list when ``passed=False`` so we can build
    the corrective prompt. ``parsed`` is the JSON object that was
    successfully decoded — None when the content wasn't even
    parseable JSON.
    """

    passed: bool
    parsed: Any = None
    errors: list[str] | None = None
    raw_content: str = ""


# --------------------------------------------------------------------------- #
# Schema extraction (OpenAI response_format)                                  #
# --------------------------------------------------------------------------- #


def extract_schema(response_format: dict[str, Any] | None) -> dict[str, Any] | None:
    """Pull the JSON Schema out of an OpenAI-shape ``response_format``.

    The OpenAI canonical shape is::

        {
            "type": "json_schema",
            "json_schema": {
                "name": "ResponseShape",
                "schema": { ... },
                "strict": true
            }
        }

    Returns the inner ``schema`` dict, or ``None`` when no schema is
    present. Other ``response_format`` shapes (``{"type": "json_object"}``
    — strict-JSON-only with no schema; ``{"type": "text"}`` — plain
    text) return None so the gateway just passes them through.
    """
    if not isinstance(response_format, dict):
        return None
    if response_format.get("type") != "json_schema":
        return None
    inner = response_format.get("json_schema")
    if not isinstance(inner, dict):
        return None
    schema = inner.get("schema")
    if not isinstance(schema, dict):
        return None
    return schema


# --------------------------------------------------------------------------- #
# Validation                                                                  #
# --------------------------------------------------------------------------- #


def validate_response_content(
    content: str | None, schema: dict[str, Any]
) -> ValidationResult:
    """Validate an assistant-message content string against ``schema``.

    Three failure modes, handled distinctly so the corrective prompt
    is helpful rather than generic:

    1. Content is ``None`` / empty — the model produced no text body
       (tool_calls only, or empty completion). Validation fails with
       a "missing response" error. The retry prompt asks for any
       response.
    2. Content isn't valid JSON — model wrapped it in prose or
       returned an invalid JSON fragment. We strip markdown fences
       first; if that still doesn't parse, fail with "not JSON" so
       the retry asks for raw JSON.
    3. Content is valid JSON but fails the schema — return per-path
       error strings.
    """
    if not content:
        return ValidationResult(
            passed=False,
            errors=["The response was empty. Provide a JSON object that matches the schema."],
            raw_content="",
        )

    raw = content.strip()
    # Strip markdown code fence if present.
    m = _FENCED_JSON_RE.match(raw)
    if m:
        raw = m.group(1).strip()

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        return ValidationResult(
            passed=False,
            errors=[
                f"The response is not valid JSON ({e.msg} at line {e.lineno} col {e.colno}). "
                "Respond with a raw JSON object, no surrounding text or markdown."
            ],
            raw_content=content,
        )

    validator = Draft202012Validator(schema)
    errors = list(validator.iter_errors(parsed))
    if not errors:
        return ValidationResult(passed=True, parsed=parsed, raw_content=content)

    formatted = _format_validation_errors(errors)
    return ValidationResult(
        passed=False, parsed=parsed, errors=formatted, raw_content=content
    )


def _format_validation_errors(errors: list[ValidationError]) -> list[str]:
    """Build per-path strings the LLM can act on.

    ``jsonschema`` produces a tree of errors; we flatten it to the
    leaf nodes (most specific path + most actionable message). The
    output is capped at ``_MAX_ERRORS_IN_PROMPT`` — beyond that the
    corrective signal degrades.
    """
    out: list[str] = []
    for err in errors[:_MAX_ERRORS_IN_PROMPT]:
        # path is a deque; join with "." for readable output.
        # Root errors get "<root>" so the message isn't ambiguous.
        path = ".".join(str(p) for p in err.absolute_path) or "<root>"
        out.append(f"{path}: {err.message}")
    if len(errors) > _MAX_ERRORS_IN_PROMPT:
        out.append(
            f"... and {len(errors) - _MAX_ERRORS_IN_PROMPT} more error(s) (truncated)."
        )
    return out


# --------------------------------------------------------------------------- #
# Corrective prompt construction                                              #
# --------------------------------------------------------------------------- #


def build_correction_messages(
    *,
    failed_response_content: str,
    errors: list[str],
    schema: dict[str, Any],
) -> list[dict[str, Any]]:
    """Build the two messages that follow a failed validation.

    Returns ``[assistant_echo, user_correction]`` — appended to the
    original ``messages`` list before the retry. The assistant echo
    is the EXACT content the model produced (including any markdown
    fence); this keeps the model anchored on its own previous output
    so the correction is incremental, not a fresh attempt with no
    context.

    The user message format is the standard "constitutional"
    correction shape: state the failure, list the specific errors,
    re-state the constraint. We include the schema in JSON form too
    — for some models, re-stating the schema in the same context
    materially helps (the original schema may have been many turns
    ago).
    """
    error_block = "\n".join(f"- {e}" for e in errors)
    schema_json = json.dumps(schema, indent=2)
    correction_text = (
        "Your previous response failed JSON Schema validation. The errors are:\n"
        f"{error_block}\n\n"
        "Please respond again with valid JSON matching this schema exactly. "
        "Do not include any surrounding text or markdown code fences — emit "
        "only the JSON object itself.\n\n"
        f"Schema:\n```json\n{schema_json}\n```"
    )
    return [
        {"role": "assistant", "content": failed_response_content or ""},
        {"role": "user", "content": correction_text},
    ]


# --------------------------------------------------------------------------- #
# Schema-guided system prompt (fallback when provider lacks native support)   #
# --------------------------------------------------------------------------- #


def build_schema_system_message(schema: dict[str, Any]) -> dict[str, Any]:
    """Build a system message that instructs the model to respect the schema.

    Used when the provider doesn't expose a native structured-output
    mechanism OR when the team has ``structured_output_provider_native=False``.
    The wording is deliberately blunt — we've found that LLMs honour
    "ONLY respond with valid JSON matching this schema, no other text"
    much more reliably than softer phrasings.
    """
    schema_json = json.dumps(schema, indent=2)
    content = (
        "You must respond with a single JSON object that conforms to the "
        "following JSON Schema. Do not include any text, explanation, or "
        "markdown code fence around the JSON — emit only the raw JSON "
        "object itself.\n\n"
        f"Schema:\n```json\n{schema_json}\n```"
    )
    return {"role": "system", "content": content}


__all__ = [
    "ValidationResult",
    "build_correction_messages",
    "build_schema_system_message",
    "extract_schema",
    "validate_response_content",
]
