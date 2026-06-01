"""Per-tool budget helpers (Phase 37).

Pronaos doesn't execute tools — the LLM emits ``tool_calls``, the
client executes them, the client sends back ``tool`` role messages
with results. So Pronaos sees:

1. The LLM's response that emitted ``tool_calls`` (tool names + args).
2. The follow-up request carrying ``tool`` role messages with results.

Per-tool budgets cap the count of tool_call EMISSIONS the LLM produces
for each tool name within the team's monthly period. Enforcement is
**strip-by-removal**: when the running count for a tool reaches its
limit, the chat handler strips that tool from the upstream request's
``tools`` array BEFORE forwarding. The LLM never sees the tool, never
attempts to call it, never wastes reasoning on something that would
get denied.

This module exposes two pure functions:

- :func:`is_over_budget` — given a team's ``tool_budgets`` and a tool
  name, returns ``True`` when the tool should be stripped.
- :func:`strip_over_budget_tools` — given a request's ``tools`` list,
  returns the filtered list + the set of names that were stripped.
"""

from __future__ import annotations

from typing import Any


def is_over_budget(
    tool_budgets: dict[str, dict[str, int]] | None,
    tool_name: str,
) -> bool:
    """Return True when ``tool_name``'s running count has reached its limit.

    Shape of ``tool_budgets`` per the Phase 37 schema:

    .. code-block:: json

        {"web_search": {"limit_calls": 100, "current_calls": 100}}

    Semantics:

    - NULL / absent tool_budgets → no caps for this team → never over budget.
    - Tool name absent from the dict → no cap on this tool → not over budget.
    - ``limit_calls`` missing or non-int → treat as no cap (defensive — a
      malformed write at the admin tier shouldn't break the request path).
    - ``current_calls`` missing → treat as 0.
    - ``current_calls >= limit_calls`` → over budget (deny by strip).
    """
    if not tool_budgets:
        return False
    entry = tool_budgets.get(tool_name)
    if not isinstance(entry, dict):
        return False
    limit_raw = entry.get("limit_calls")
    if not isinstance(limit_raw, int) or limit_raw <= 0:
        return False
    current_raw = entry.get("current_calls", 0)
    current = current_raw if isinstance(current_raw, int) else 0
    return current >= limit_raw


def strip_over_budget_tools(
    tools: list[dict[str, Any]] | None,
    tool_budgets: dict[str, dict[str, int]] | None,
) -> tuple[list[dict[str, Any]] | None, list[str]]:
    """Filter ``tools`` to remove over-budget entries.

    Returns ``(new_tools, stripped_names)``. ``new_tools`` preserves
    order; if every tool was stripped, returns ``[]`` (caller decides
    whether to set ``None`` or send an empty list to the upstream).

    Tool name extraction handles the OpenAI shape we use throughout
    the gateway: ``{"type":"function","function":{"name":"foo",...}}``.
    Entries with a missing or non-string name are kept (we can't
    correlate them to budget entries — they pass through unchanged).
    """
    if not tools:
        return tools, []
    if not tool_budgets:
        return tools, []

    new_tools: list[dict[str, Any]] = []
    stripped: list[str] = []
    for t in tools:
        name = _extract_tool_name(t)
        if name and is_over_budget(tool_budgets, name):
            stripped.append(name)
            continue
        new_tools.append(t)
    return new_tools, stripped


def _extract_tool_name(tool: dict[str, Any]) -> str | None:
    """Pull the function name out of an OpenAI-shape tool definition.

    Defensive against malformed entries — we'd rather pass through an
    unrecognised tool than crash the request.
    """
    fn = tool.get("function")
    if not isinstance(fn, dict):
        return None
    name = fn.get("name")
    return name if isinstance(name, str) else None


def tool_names_from_calls(
    tool_calls: list[dict[str, Any]] | None,
) -> list[str]:
    """Extract the tool names from an LLM response's ``tool_calls``.

    Used by the chat handler to:
    1. Increment per-tool metrics + budgets.
    2. Persist to ``usage_records.tool_names`` / ``audit_records.tool_names``.

    Handles the OpenAI-shape we normalise to throughout the gateway:
    ``[{"id":..., "type":"function", "function":{"name":"foo", ...}}, ...]``.
    Returns an empty list when there are no tool_calls or the list is
    malformed. Duplicates are preserved (the LLM might call the same
    tool twice in one response — each is a separate invocation for
    metric and budget purposes).
    """
    if not tool_calls:
        return []
    names: list[str] = []
    for tc in tool_calls:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function")
        if not isinstance(fn, dict):
            continue
        name = fn.get("name")
        if isinstance(name, str) and name:
            names.append(name)
    return names
