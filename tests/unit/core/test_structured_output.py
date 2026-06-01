"""Unit tests for the Phase 39 structured-output validation helpers.

Two pure-function surfaces under test:

1. ``extract_schema`` — pulls the JSON Schema dict out of an
   OpenAI-shape ``response_format``. Handles missing keys gracefully
   so a malformed client payload doesn't crash the request.
2. ``validate_response_content`` — three distinct failure modes (empty,
   non-JSON, schema-violating); each must produce useful errors for
   the corrective prompt.
3. ``build_correction_messages`` — the [assistant_echo, user_correction]
   pair appended before a retry. Deterministic output so tests can
   assert exact shape.

Also tests:
4. ``build_schema_system_message`` — the prompt-injection fallback
   for providers without native structured-output support.
"""

from __future__ import annotations

import json

from pronaos.core.structured_output import (
    build_correction_messages,
    build_schema_system_message,
    extract_schema,
    validate_response_content,
)

# --------------------------------------------------------------------------- #
# extract_schema                                                              #
# --------------------------------------------------------------------------- #


class TestExtractSchema:
    def test_canonical_openai_shape(self) -> None:
        rf = {
            "type": "json_schema",
            "json_schema": {
                "name": "Address",
                "schema": {"type": "object", "properties": {"city": {"type": "string"}}},
                "strict": True,
            },
        }
        out = extract_schema(rf)
        assert out is not None
        assert out["type"] == "object"
        assert "properties" in out

    def test_none_returns_none(self) -> None:
        assert extract_schema(None) is None

    def test_wrong_type_returns_none(self) -> None:
        # type=json_object has no schema attached.
        assert extract_schema({"type": "json_object"}) is None

    def test_missing_json_schema_returns_none(self) -> None:
        assert extract_schema({"type": "json_schema"}) is None

    def test_malformed_inner_returns_none(self) -> None:
        assert extract_schema({"type": "json_schema", "json_schema": "garbage"}) is None
        assert extract_schema({"type": "json_schema", "json_schema": {}}) is None


# --------------------------------------------------------------------------- #
# validate_response_content                                                   #
# --------------------------------------------------------------------------- #


_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "age": {"type": "integer", "minimum": 0},
    },
    "required": ["name", "age"],
    "additionalProperties": False,
}


class TestValidateResponseContent:
    def test_valid_json_passes(self) -> None:
        out = validate_response_content('{"name": "Alice", "age": 30}', _SCHEMA)
        assert out.passed is True
        assert out.parsed == {"name": "Alice", "age": 30}
        assert out.errors is None

    def test_empty_content_fails_with_useful_error(self) -> None:
        out = validate_response_content("", _SCHEMA)
        assert out.passed is False
        assert out.errors is not None
        assert "empty" in out.errors[0].lower()

    def test_none_content_fails_with_useful_error(self) -> None:
        out = validate_response_content(None, _SCHEMA)
        assert out.passed is False
        assert out.errors is not None

    def test_non_json_text_fails_with_useful_error(self) -> None:
        out = validate_response_content("not json at all", _SCHEMA)
        assert out.passed is False
        assert out.errors is not None
        assert "not valid JSON" in out.errors[0]

    def test_markdown_fenced_json_is_stripped(self) -> None:
        """A model that wraps JSON in ```json ... ``` still validates —
        we strip the fence before parsing."""
        content = '```json\n{"name": "Bob", "age": 25}\n```'
        out = validate_response_content(content, _SCHEMA)
        assert out.passed is True
        assert out.parsed == {"name": "Bob", "age": 25}

    def test_plain_fence_without_language(self) -> None:
        """``` without ``json`` is also a common pattern."""
        content = '```\n{"name": "Carol", "age": 40}\n```'
        out = validate_response_content(content, _SCHEMA)
        assert out.passed is True

    def test_missing_required_field_fails(self) -> None:
        out = validate_response_content('{"name": "Dave"}', _SCHEMA)
        assert out.passed is False
        assert out.errors is not None
        # The error mentions 'age' being required.
        assert any("age" in e for e in out.errors)

    def test_wrong_type_fails(self) -> None:
        out = validate_response_content('{"name": "Eve", "age": "thirty"}', _SCHEMA)
        assert out.passed is False
        assert out.errors is not None
        # The error path points at 'age'.
        assert any("age" in e for e in out.errors)

    def test_additional_property_fails_when_disallowed(self) -> None:
        # additionalProperties: False on the schema → extra fields fail.
        out = validate_response_content(
            '{"name": "Frank", "age": 50, "extra": "field"}', _SCHEMA
        )
        assert out.passed is False
        assert out.errors is not None

    def test_constraint_violation_fails(self) -> None:
        # age has minimum=0, so negative fails.
        out = validate_response_content('{"name": "Greta", "age": -5}', _SCHEMA)
        assert out.passed is False
        assert out.errors is not None
        assert any("age" in e for e in out.errors)


# --------------------------------------------------------------------------- #
# build_correction_messages                                                   #
# --------------------------------------------------------------------------- #


class TestBuildCorrectionMessages:
    def test_returns_assistant_then_user(self) -> None:
        msgs = build_correction_messages(
            failed_response_content='{"foo": "bar"}',
            errors=["age: required field missing"],
            schema=_SCHEMA,
        )
        assert len(msgs) == 2
        assert msgs[0]["role"] == "assistant"
        assert msgs[0]["content"] == '{"foo": "bar"}'
        assert msgs[1]["role"] == "user"

    def test_correction_message_carries_errors(self) -> None:
        msgs = build_correction_messages(
            failed_response_content="bad",
            errors=["root: not object", "extra error"],
            schema=_SCHEMA,
        )
        correction = msgs[1]["content"]
        assert "root: not object" in correction
        assert "extra error" in correction
        # Schema is re-included to anchor the model's next attempt.
        assert "Schema" in correction

    def test_empty_failed_content_uses_empty_string(self) -> None:
        msgs = build_correction_messages(
            failed_response_content="",
            errors=["empty"],
            schema=_SCHEMA,
        )
        assert msgs[0]["content"] == ""

    def test_correction_includes_schema_as_json(self) -> None:
        msgs = build_correction_messages(
            failed_response_content="",
            errors=["any"],
            schema=_SCHEMA,
        )
        correction = msgs[1]["content"]
        # The schema is serialised with indent so the model can read it.
        # Round-trip parse the embedded JSON to confirm it's well-formed.
        start = correction.index("```json\n") + len("```json\n")
        end = correction.rindex("\n```")
        embedded = correction[start:end]
        assert json.loads(embedded) == _SCHEMA


# --------------------------------------------------------------------------- #
# build_schema_system_message                                                 #
# --------------------------------------------------------------------------- #


class TestBuildSchemaSystemMessage:
    def test_returns_system_role(self) -> None:
        msg = build_schema_system_message(_SCHEMA)
        assert msg["role"] == "system"

    def test_carries_schema_in_body(self) -> None:
        msg = build_schema_system_message(_SCHEMA)
        # The schema is serialised in the message body so the model
        # can see the constraints.
        assert "type" in msg["content"]
        assert "properties" in msg["content"]
