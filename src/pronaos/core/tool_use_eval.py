"""BFCL-style tool-use accuracy evaluator (Phase 45).

The Berkeley Function-Calling Leaderboard measures whether a model
invokes the correct tool with the correct arguments for a given
prompt. Phase 24 measures *answer quality* via judge-scoring; this
module measures *tool-call accuracy* — a different dimension that
agent-platform engineering cares about a lot.

Scoring rules
-------------
A case passes iff:

1. The model's response matches the case's expectation shape:
   - ``expected_function`` is a string  → exactly one tool call,
     matching name + arguments
   - ``expected_function`` is None      → no tool call at all
     (the model should respond with text only)
   - ``expected_parallel`` is non-empty → two or more tool calls,
     each matching one of the expected (function, args) tuples
     (order-independent; uses multiset matching)

2. The argument dict matches in canonical form:
   - Key order doesn't matter
   - Integer/float pairs are treated as equal when numerically equal
     (e.g. ``5 == 5.0``)
   - String values must match exactly (case-sensitive)
   - Nested dicts and lists compared recursively
   - Extra keys in the model's output FAIL the case (the spec only
     listed the required ones; injecting extras is a model error)
   - Missing required keys FAIL the case

Pure functions, no I/O. The runner script handles the gateway-side
HTTP plumbing.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

# --------------------------------------------------------------------------- #
# Case + result types                                                         #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ToolUseCase:
    """One BFCL-style evaluation case.

    Categories: ``simple``, ``selection``, ``arguments``, ``relevance``,
    ``parallel`` — see the YAML fixture's docstring.
    """

    case_id: str
    category: str
    prompt: str
    tools: list[dict[str, Any]]
    expected_function: str | None
    expected_args: dict[str, Any]
    expected_parallel: list[dict[str, Any]] = field(default_factory=list)
    tags: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class ToolUseScore:
    """Per-case verdict produced by :func:`score_case`.

    ``passed`` is the single bit the runner aggregates. ``reason``
    is a short string for the failure mode — "wrong_function",
    "wrong_args", "unexpected_call", "missing_call", "extra_keys",
    "wrong_call_count" — surfaced in the runner output so the
    operator can see WHY a model failed each case, not just that
    it did.
    """

    case_id: str
    passed: bool
    reason: str = ""
    observed_function: str | None = None
    observed_args: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ToolUseSummary:
    """Per-model aggregate over a run."""

    model: str
    total: int
    passed: int
    by_category: dict[str, tuple[int, int]] = field(default_factory=dict)
    # Each entry: (case_id, passed, reason). Stays in document order
    # so the runner can print the failures inline with the case ids.
    per_case: list[tuple[str, bool, str]] = field(default_factory=list)

    @property
    def accuracy(self) -> float:
        return self.passed / self.total if self.total else 0.0


# --------------------------------------------------------------------------- #
# Argument canonicalisation                                                   #
# --------------------------------------------------------------------------- #


def _canonical(value: Any) -> Any:
    """Reduce a JSON-shaped value to a comparable canonical form.

    - ``bool`` stays distinct from ``int`` (True != 1 in canonical
      form even though Python treats them as equal).
    - ``int`` / ``float`` are coerced to float when numerically equal
      so 5 and 5.0 compare equal.
    - Strings are stripped of surrounding whitespace and compared
      case-sensitive otherwise.
    - Dicts and lists are recursively canonicalised.

    Returns a hashable representation (tuples for dicts so they
    can be inserted into multisets for parallel-call matching).
    """
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, int | float):
        return ("num", float(value))
    if isinstance(value, str):
        return ("str", value.strip())
    if isinstance(value, dict):
        return tuple(sorted((k, _canonical(v)) for k, v in value.items()))
    if isinstance(value, list | tuple):
        return tuple(_canonical(v) for v in value)
    return value


def args_equal(observed: dict[str, Any], expected: dict[str, Any]) -> bool:
    """Compare two argument dicts in canonical form.

    Extra keys in ``observed`` fail the comparison — the case spec
    defines the contract.
    """
    if set(observed.keys()) != set(expected.keys()):
        return False
    return bool(_canonical(observed) == _canonical(expected))


# --------------------------------------------------------------------------- #
# Tool-call extraction from a gateway response                                #
# --------------------------------------------------------------------------- #


def extract_tool_calls(response_body: dict[str, Any]) -> list[dict[str, Any]]:
    """Pull the OpenAI-shape tool_calls list out of a chat response.

    Returns a list of ``{"name": str, "arguments": dict}`` entries.
    Arguments are JSON-decoded so the scorer doesn't have to do it.
    On a malformed arguments JSON string we return an empty dict for
    that call — the scorer will then fail the case with a clean
    ``wrong_args`` reason rather than crashing.
    """
    out: list[dict[str, Any]] = []
    choices = response_body.get("choices")
    if not isinstance(choices, list) or not choices:
        return out
    msg = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(msg, dict):
        return out
    tcs = msg.get("tool_calls")
    if not isinstance(tcs, list):
        return out
    for tc in tcs:
        if not isinstance(tc, dict):
            continue
        func = tc.get("function")
        if not isinstance(func, dict):
            continue
        name = func.get("name")
        if not isinstance(name, str):
            continue
        raw_args = func.get("arguments")
        parsed: dict[str, Any] = {}
        if isinstance(raw_args, dict):
            parsed = raw_args
        elif isinstance(raw_args, str):
            try:
                decoded = json.loads(raw_args)
                if isinstance(decoded, dict):
                    parsed = decoded
            except json.JSONDecodeError:
                parsed = {}
        out.append({"name": name, "arguments": parsed})
    return out


# --------------------------------------------------------------------------- #
# Per-case scorer                                                             #
# --------------------------------------------------------------------------- #


def score_case(case: ToolUseCase, response_body: dict[str, Any]) -> ToolUseScore:
    """Apply the scoring rules to one (case, response) pair.

    See module docstring for the full rule list. Returns a
    ``ToolUseScore`` with ``passed`` + ``reason`` populated.
    """
    tool_calls = extract_tool_calls(response_body)

    if case.expected_parallel:
        return _score_parallel(case, tool_calls)

    if case.expected_function is None:
        return _score_relevance(case, tool_calls)

    return _score_simple(case, tool_calls)


def _score_simple(case: ToolUseCase, tool_calls: list[dict[str, Any]]) -> ToolUseScore:
    """Single-call case: exactly one tool call with the right name + args."""
    if not tool_calls:
        return ToolUseScore(case_id=case.case_id, passed=False, reason="missing_call")
    if len(tool_calls) > 1:
        return ToolUseScore(
            case_id=case.case_id,
            passed=False,
            reason="wrong_call_count",
            observed_function=tool_calls[0].get("name"),
            observed_args=tool_calls[0].get("arguments", {}),
        )
    call = tool_calls[0]
    name = call.get("name")
    args = call.get("arguments", {})
    if name != case.expected_function:
        return ToolUseScore(
            case_id=case.case_id,
            passed=False,
            reason="wrong_function",
            observed_function=name,
            observed_args=args,
        )
    if not args_equal(args, case.expected_args):
        return ToolUseScore(
            case_id=case.case_id,
            passed=False,
            reason="wrong_args",
            observed_function=name,
            observed_args=args,
        )
    return ToolUseScore(
        case_id=case.case_id,
        passed=True,
        observed_function=name,
        observed_args=args,
    )


def _score_relevance(case: ToolUseCase, tool_calls: list[dict[str, Any]]) -> ToolUseScore:
    """Relevance case: NO tool should be called."""
    if not tool_calls:
        return ToolUseScore(case_id=case.case_id, passed=True)
    return ToolUseScore(
        case_id=case.case_id,
        passed=False,
        reason="unexpected_call",
        observed_function=tool_calls[0].get("name"),
        observed_args=tool_calls[0].get("arguments", {}),
    )


def _score_parallel(case: ToolUseCase, tool_calls: list[dict[str, Any]]) -> ToolUseScore:
    """Parallel case: every expected (function, args) must appear
    exactly once in the observed list, no extras."""
    expected = list(case.expected_parallel)
    if len(tool_calls) != len(expected):
        first_name = tool_calls[0].get("name") if tool_calls else None
        return ToolUseScore(
            case_id=case.case_id,
            passed=False,
            reason="wrong_call_count",
            observed_function=first_name,
        )
    # Match each observed call to an expected entry; we use
    # a list-as-multiset approach (remove matched entries).
    remaining = [dict(e) for e in expected]
    for call in tool_calls:
        match_idx = -1
        for i, exp in enumerate(remaining):
            exp_name = exp.get("function")
            exp_args = exp.get("args") or {}
            if call.get("name") == exp_name and args_equal(call.get("arguments", {}), exp_args):
                match_idx = i
                break
        if match_idx < 0:
            return ToolUseScore(
                case_id=case.case_id,
                passed=False,
                reason="wrong_function"
                if not _has_call_with_name(remaining, call.get("name"))
                else "wrong_args",
                observed_function=call.get("name"),
                observed_args=call.get("arguments", {}),
            )
        remaining.pop(match_idx)
    return ToolUseScore(case_id=case.case_id, passed=True)


def _has_call_with_name(remaining: Sequence[dict[str, Any]], name: Any) -> bool:
    return any(exp.get("function") == name for exp in remaining)


# --------------------------------------------------------------------------- #
# Aggregate summary                                                           #
# --------------------------------------------------------------------------- #


def summarize(
    model: str, cases: Sequence[ToolUseCase], scores: Sequence[ToolUseScore]
) -> ToolUseSummary:
    """Aggregate per-case scores into a per-model summary."""
    total = len(scores)
    passed = sum(1 for s in scores if s.passed)
    by_category: dict[str, tuple[int, int]] = {}
    per_case: list[tuple[str, bool, str]] = []
    score_by_id = {s.case_id: s for s in scores}
    for c in cases:
        s = score_by_id.get(c.case_id)
        if s is None:
            continue
        per_case.append((c.case_id, s.passed, s.reason))
        cat_p, cat_t = by_category.get(c.category, (0, 0))
        by_category[c.category] = (cat_p + (1 if s.passed else 0), cat_t + 1)
    return ToolUseSummary(
        model=model,
        total=total,
        passed=passed,
        by_category=by_category,
        per_case=per_case,
    )


# --------------------------------------------------------------------------- #
# Golden-set loader                                                           #
# --------------------------------------------------------------------------- #


def load_golden_set(path: str) -> list[ToolUseCase]:
    """Read a YAML golden set off disk into a list of ToolUseCase.

    The YAML shape matches ``tests/eval/data/tool_use_basic.yaml``.
    Defensive: malformed entries raise ``ValueError`` with the
    offending case_id so the operator can fix the YAML.
    """
    import yaml  # lazy import so this module is usable without PyYAML

    with open(path, encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    cases_raw = doc.get("cases") if isinstance(doc, dict) else None
    if not isinstance(cases_raw, list):
        raise ValueError(f"{path}: no 'cases' list at the top level")
    out: list[ToolUseCase] = []
    for entry in cases_raw:
        if not isinstance(entry, dict):
            raise ValueError(f"{path}: case is not a mapping: {entry!r}")
        case_id = entry.get("id")
        if not isinstance(case_id, str) or not case_id:
            raise ValueError(f"{path}: case missing 'id': {entry!r}")
        category = entry.get("category", "uncategorized")
        prompt = entry.get("prompt", "")
        tools = entry.get("tools") or []
        expected_function = entry.get("expected_function")
        expected_args = entry.get("expected_args") or {}
        expected_parallel = entry.get("expected_parallel") or []
        tags = tuple(entry.get("tags") or ())
        out.append(
            ToolUseCase(
                case_id=case_id,
                category=str(category),
                prompt=str(prompt),
                tools=list(tools),
                expected_function=expected_function,
                expected_args=dict(expected_args),
                expected_parallel=list(expected_parallel),
                tags=tags,
            )
        )
    return out
