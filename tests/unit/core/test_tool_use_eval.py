"""Unit tests for the BFCL-style tool-use scorer (Phase 45).

Four surfaces under test:

1. **Argument canonicalisation** (``args_equal``) — int/float coercion,
   key order, nested dicts, extra keys, missing keys.
2. **Tool-call extraction** (``extract_tool_calls``) — pull the
   OpenAI-shape ``tool_calls`` list out of a gateway response,
   JSON-decode arguments, handle malformed entries gracefully.
3. **Per-case scoring** (``score_case``) — simple, selection,
   arguments, relevance, parallel categories. Each failure mode
   surfaces a clean ``reason`` string.
4. **Summary aggregation** (``summarize``) — per-category breakdown,
   per-case list in document order.
"""

from __future__ import annotations

import json

from pronaos.core.tool_use_eval import (
    ToolUseCase,
    args_equal,
    extract_tool_calls,
    score_case,
    summarize,
)


def _resp(tool_calls: list[dict] | None = None, content: str = "") -> dict:
    """Build a minimal chat-completion response with the given tool calls."""
    msg: dict = {"role": "assistant", "content": content}
    if tool_calls is not None:
        msg["tool_calls"] = tool_calls
    return {
        "id": "chatcmpl-test",
        "choices": [{"index": 0, "message": msg, "finish_reason": "tool_calls" if tool_calls else "stop"}],
    }


def _tool_call(name: str, args: dict) -> dict:
    return {
        "id": "call_x",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


# --------------------------------------------------------------------------- #
# Argument canonicalisation                                                   #
# --------------------------------------------------------------------------- #


class TestArgsEqual:
    def test_exact_match(self) -> None:
        assert args_equal({"city": "Paris"}, {"city": "Paris"}) is True

    def test_key_order_independent(self) -> None:
        assert args_equal({"a": 1, "b": 2}, {"b": 2, "a": 1}) is True

    def test_int_float_coerced(self) -> None:
        assert args_equal({"x": 5}, {"x": 5.0}) is True
        assert args_equal({"x": 45}, {"x": 45.0}) is True

    def test_string_case_sensitive(self) -> None:
        assert args_equal({"city": "paris"}, {"city": "Paris"}) is False

    def test_string_whitespace_stripped(self) -> None:
        assert args_equal({"city": " Paris "}, {"city": "Paris"}) is True

    def test_extra_keys_fail(self) -> None:
        """Extra keys in observed (model output) fail the match.
        The case contract is exact."""
        assert args_equal({"city": "Paris", "extra": "x"}, {"city": "Paris"}) is False

    def test_missing_keys_fail(self) -> None:
        assert args_equal({}, {"city": "Paris"}) is False

    def test_nested_dict_recursive(self) -> None:
        assert args_equal(
            {"loc": {"lat": 48.8, "lng": 2.3}},
            {"loc": {"lng": 2.3, "lat": 48.8}},
        ) is True

    def test_list_value(self) -> None:
        assert args_equal({"tags": ["a", "b"]}, {"tags": ["a", "b"]}) is True

    def test_bool_distinct_from_int(self) -> None:
        """True and 1 are equal in Python; canonical form keeps them
        distinct so a model that outputs True instead of 1 fails."""
        assert args_equal({"flag": True}, {"flag": 1}) is False


# --------------------------------------------------------------------------- #
# Tool-call extraction                                                        #
# --------------------------------------------------------------------------- #


class TestExtractToolCalls:
    def test_empty_response(self) -> None:
        assert extract_tool_calls({}) == []
        assert extract_tool_calls({"choices": []}) == []

    def test_no_tool_calls(self) -> None:
        body = _resp(tool_calls=None, content="just text")
        assert extract_tool_calls(body) == []

    def test_single_tool_call(self) -> None:
        body = _resp(tool_calls=[_tool_call("get_weather", {"city": "Paris"})])
        out = extract_tool_calls(body)
        assert out == [{"name": "get_weather", "arguments": {"city": "Paris"}}]

    def test_multiple_tool_calls(self) -> None:
        body = _resp(
            tool_calls=[
                _tool_call("get_weather", {"city": "Paris"}),
                _tool_call("get_weather", {"city": "London"}),
            ]
        )
        out = extract_tool_calls(body)
        assert len(out) == 2
        assert out[1] == {"name": "get_weather", "arguments": {"city": "London"}}

    def test_malformed_arguments_string_returns_empty_dict(self) -> None:
        """A model that returns invalid JSON in arguments gets an empty
        dict — the case will then fail with a clean ``wrong_args``
        reason rather than crashing the runner."""
        body = _resp(
            tool_calls=[
                {
                    "id": "call_x",
                    "type": "function",
                    "function": {"name": "f", "arguments": "{not json"},
                }
            ]
        )
        assert extract_tool_calls(body) == [{"name": "f", "arguments": {}}]

    def test_dict_arguments_passed_through(self) -> None:
        """Some adapters might return arguments as a dict directly
        rather than a JSON-encoded string. Accept both."""
        body = _resp(
            tool_calls=[
                {
                    "id": "call_x",
                    "type": "function",
                    "function": {"name": "f", "arguments": {"x": 1}},
                }
            ]
        )
        assert extract_tool_calls(body) == [{"name": "f", "arguments": {"x": 1}}]


# --------------------------------------------------------------------------- #
# score_case                                                                  #
# --------------------------------------------------------------------------- #


def _case(
    *,
    case_id: str = "c1",
    category: str = "simple",
    expected_function: str | None = "get_weather",
    expected_args: dict | None = None,
    expected_parallel: list[dict] | None = None,
) -> ToolUseCase:
    return ToolUseCase(
        case_id=case_id,
        category=category,
        prompt="test",
        tools=[],
        expected_function=expected_function,
        expected_args=expected_args or {"city": "Paris"},
        expected_parallel=expected_parallel or [],
    )


class TestScoreSimple:
    def test_correct_call_passes(self) -> None:
        case = _case()
        body = _resp(tool_calls=[_tool_call("get_weather", {"city": "Paris"})])
        result = score_case(case, body)
        assert result.passed is True
        assert result.reason == ""

    def test_missing_call_fails(self) -> None:
        case = _case()
        body = _resp(tool_calls=None, content="It's nice in Paris.")
        result = score_case(case, body)
        assert result.passed is False
        assert result.reason == "missing_call"

    def test_wrong_function_fails(self) -> None:
        case = _case()
        body = _resp(tool_calls=[_tool_call("book_flight", {"destination": "Paris"})])
        result = score_case(case, body)
        assert result.passed is False
        assert result.reason == "wrong_function"
        assert result.observed_function == "book_flight"

    def test_wrong_args_fails(self) -> None:
        case = _case()
        body = _resp(tool_calls=[_tool_call("get_weather", {"city": "Tokyo"})])
        result = score_case(case, body)
        assert result.passed is False
        assert result.reason == "wrong_args"

    def test_extra_args_fail(self) -> None:
        case = _case()
        body = _resp(
            tool_calls=[
                _tool_call("get_weather", {"city": "Paris", "extra": "yes"})
            ]
        )
        result = score_case(case, body)
        assert result.passed is False
        assert result.reason == "wrong_args"

    def test_multiple_calls_when_one_expected_fails(self) -> None:
        case = _case()
        body = _resp(
            tool_calls=[
                _tool_call("get_weather", {"city": "Paris"}),
                _tool_call("get_weather", {"city": "London"}),
            ]
        )
        result = score_case(case, body)
        assert result.passed is False
        assert result.reason == "wrong_call_count"


class TestScoreRelevance:
    def test_no_call_passes(self) -> None:
        case = _case(category="relevance", expected_function=None, expected_args={})
        body = _resp(tool_calls=None, content="Just chatting!")
        result = score_case(case, body)
        assert result.passed is True

    def test_empty_tool_calls_list_passes(self) -> None:
        case = _case(category="relevance", expected_function=None, expected_args={})
        # Some adapters emit tool_calls: [] for "no calls"; treat as
        # equivalent to no tool_calls key.
        body = _resp(tool_calls=[], content="Just chatting!")
        result = score_case(case, body)
        assert result.passed is True

    def test_unexpected_call_fails(self) -> None:
        case = _case(category="relevance", expected_function=None, expected_args={})
        body = _resp(tool_calls=[_tool_call("get_weather", {"city": "Paris"})])
        result = score_case(case, body)
        assert result.passed is False
        assert result.reason == "unexpected_call"


class TestScoreParallel:
    def test_both_calls_correct_passes(self) -> None:
        case = _case(
            category="parallel",
            expected_function=None,
            expected_args={},
            expected_parallel=[
                {"function": "get_weather", "args": {"city": "Paris"}},
                {"function": "get_weather", "args": {"city": "London"}},
            ],
        )
        body = _resp(
            tool_calls=[
                _tool_call("get_weather", {"city": "Paris"}),
                _tool_call("get_weather", {"city": "London"}),
            ]
        )
        result = score_case(case, body)
        assert result.passed is True

    def test_calls_in_different_order_pass(self) -> None:
        """Parallel matching is order-independent."""
        case = _case(
            category="parallel",
            expected_function=None,
            expected_args={},
            expected_parallel=[
                {"function": "get_weather", "args": {"city": "Paris"}},
                {"function": "get_weather", "args": {"city": "London"}},
            ],
        )
        body = _resp(
            tool_calls=[
                _tool_call("get_weather", {"city": "London"}),
                _tool_call("get_weather", {"city": "Paris"}),
            ]
        )
        result = score_case(case, body)
        assert result.passed is True

    def test_only_one_call_fails(self) -> None:
        case = _case(
            category="parallel",
            expected_function=None,
            expected_args={},
            expected_parallel=[
                {"function": "get_weather", "args": {"city": "Paris"}},
                {"function": "get_weather", "args": {"city": "London"}},
            ],
        )
        body = _resp(tool_calls=[_tool_call("get_weather", {"city": "Paris"})])
        result = score_case(case, body)
        assert result.passed is False
        assert result.reason == "wrong_call_count"

    def test_wrong_args_in_parallel_fails(self) -> None:
        case = _case(
            category="parallel",
            expected_function=None,
            expected_args={},
            expected_parallel=[
                {"function": "get_weather", "args": {"city": "Paris"}},
                {"function": "get_weather", "args": {"city": "London"}},
            ],
        )
        body = _resp(
            tool_calls=[
                _tool_call("get_weather", {"city": "Paris"}),
                _tool_call("get_weather", {"city": "Tokyo"}),  # wrong city
            ]
        )
        result = score_case(case, body)
        assert result.passed is False
        # Tokyo isn't an expected target, but the FUNCTION name is right —
        # we surface that as wrong_args (the call_count matched, the
        # contents didn't).
        assert result.reason == "wrong_args"


# --------------------------------------------------------------------------- #
# summarize                                                                   #
# --------------------------------------------------------------------------- #


class TestSummarize:
    def test_aggregate_accuracy(self) -> None:
        from pronaos.core.tool_use_eval import ToolUseScore

        cases = [
            _case(case_id="a", category="simple"),
            _case(case_id="b", category="simple"),
            _case(case_id="c", category="relevance", expected_function=None),
        ]
        scores = [
            ToolUseScore(case_id="a", passed=True),
            ToolUseScore(case_id="b", passed=False, reason="wrong_args"),
            ToolUseScore(case_id="c", passed=True),
        ]
        out = summarize("groq/llama-3.1-8b-instant", cases, scores)
        assert out.total == 3
        assert out.passed == 2
        assert out.accuracy == pytest.approx(2 / 3)
        # Per-category: simple = 1/2, relevance = 1/1.
        assert out.by_category["simple"] == (1, 2)
        assert out.by_category["relevance"] == (1, 1)

    def test_per_case_order_matches_input(self) -> None:
        from pronaos.core.tool_use_eval import ToolUseScore

        cases = [_case(case_id="a"), _case(case_id="b"), _case(case_id="c")]
        scores = [
            ToolUseScore(case_id="c", passed=True),
            ToolUseScore(case_id="a", passed=False, reason="wrong_function"),
            ToolUseScore(case_id="b", passed=True),
        ]
        out = summarize("test", cases, scores)
        # per_case mirrors the document order of the input cases, not
        # the (possibly shuffled) score arrival order.
        assert [pc[0] for pc in out.per_case] == ["a", "b", "c"]


import pytest  # noqa: E402  — pytest.approx used above; placed here so the file
# can be read top-down without pytest import drama at module top.
