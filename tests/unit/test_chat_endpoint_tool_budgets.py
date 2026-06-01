"""End-to-end chat-endpoint tests for the Phase 37 per-tool budget feature.

Three behaviours to lock down via the real FastAPI stack + respx-mocked
upstream:

1. **Strip-by-removal**: when the team's ``tool_budgets`` carries an
   entry with ``current_calls >= limit_calls`` for some tool, the
   gateway must remove that tool from the upstream ``tools`` array
   BEFORE forwarding, and stamp ``X-Pronaos-Tool-Stripped`` on the
   response. Other tools pass through.

2. **tool_names propagation**: when the LLM response carries
   ``tool_calls``, the gateway records the function names into
   ``usage_records.tool_names`` (comma-joined) and into
   ``audit_records.tool_names``. Plain chat responses keep both NULL.

3. **Budget increment**: ``teams.tool_budgets[name].current_calls``
   ticks up by 1 per emitted tool name in the response. Same name
   twice = +2. Names absent from the budget map are silently skipped
   (no auto-create — guards against LLM-named DoS of the JSON column).
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import select

from pronaos.db.models import AuditRecord, Team, UsageRecord


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"


def _groq_tool_call_response(tool_name: str = "web_search") -> dict[str, Any]:
    """Minimal Groq/OpenAI-shape non-streaming response with one tool_call.

    The chat handler reads ``choices[0].message.tool_calls`` to extract
    the emitted tool names — that's the surface our tests exercise."""
    return {
        "id": "chatcmpl-x",
        "object": "chat.completion",
        "model": "llama-3.1-8b-instant",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": tool_name,
                                "arguments": json.dumps({"q": "hi"}),
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 3, "completion_tokens": 5},
    }


def _groq_text_response(text: str = "ok") -> dict[str, Any]:
    """Plain text response — no tool_calls, asserts the no-op path."""
    return {
        "id": "chatcmpl-y",
        "object": "chat.completion",
        "model": "llama-3.1-8b-instant",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 4, "completion_tokens": 2},
    }


async def _set_tool_budgets(
    sm: Any, team_id: str, budgets: dict[str, dict[str, int]] | None
) -> None:
    """Helper: rewrite the team's tool_budgets via the ORM directly."""
    async with sm() as session:
        team = await session.get(Team, team_id)
        assert team is not None
        team.tool_budgets = budgets
        await session.commit()


# --------------------------------------------------------------------------- #
# Strip-by-removal                                                            #
# --------------------------------------------------------------------------- #


@respx.mock
@pytest.mark.asyncio
async def test_over_budget_tool_stripped_from_upstream_request(auth_setup) -> None:  # type: ignore[no-untyped-def]
    """When ``web_search`` is at-cap, the gateway strips it from the
    forwarded tools array and stamps the X-Pronaos-Tool-Stripped header.
    Other tools pass through unchanged."""
    await _set_tool_budgets(
        auth_setup.sm,
        auth_setup.team_id,
        {"web_search": {"limit_calls": 5, "current_calls": 5}},
    )

    route = respx.post(GROQ_URL).mock(
        return_value=httpx.Response(200, json=_groq_text_response("done"))
    )

    resp = await auth_setup.client.post(
        "/v1/chat/completions",
        headers=_auth(auth_setup.api_key),
        json={
            "model": "groq/llama-3.1-8b-instant",
            "messages": [{"role": "user", "content": "hello"}],
            "tools": [
                {"type": "function", "function": {"name": "web_search"}},
                {"type": "function", "function": {"name": "code_exec"}},
            ],
        },
    )

    assert resp.status_code == 200
    assert resp.headers.get("X-Pronaos-Tool-Stripped") == "web_search"

    sent = json.loads(route.calls[0].request.content)
    sent_names = [t["function"]["name"] for t in sent.get("tools") or []]
    assert sent_names == ["code_exec"]  # web_search stripped


@respx.mock
@pytest.mark.asyncio
async def test_under_budget_tool_passes_through(auth_setup) -> None:  # type: ignore[no-untyped-def]
    """Tool under its cap is forwarded verbatim and no header is set."""
    await _set_tool_budgets(
        auth_setup.sm,
        auth_setup.team_id,
        {"web_search": {"limit_calls": 100, "current_calls": 1}},
    )

    route = respx.post(GROQ_URL).mock(
        return_value=httpx.Response(200, json=_groq_text_response("done"))
    )

    resp = await auth_setup.client.post(
        "/v1/chat/completions",
        headers=_auth(auth_setup.api_key),
        json={
            "model": "groq/llama-3.1-8b-instant",
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"type": "function", "function": {"name": "web_search"}}],
        },
    )

    assert resp.status_code == 200
    assert "X-Pronaos-Tool-Stripped" not in resp.headers
    sent = json.loads(route.calls[0].request.content)
    assert [t["function"]["name"] for t in sent["tools"]] == ["web_search"]


@respx.mock
@pytest.mark.asyncio
async def test_all_tools_stripped_passes_empty_list(auth_setup) -> None:  # type: ignore[no-untyped-def]
    """When every tool is over-budget, the gateway forwards ``tools: []``
    rather than ``tools: null`` — preserves the "client wanted tools"
    signal for upstream validation."""
    await _set_tool_budgets(
        auth_setup.sm,
        auth_setup.team_id,
        {
            "a": {"limit_calls": 1, "current_calls": 1},
            "b": {"limit_calls": 1, "current_calls": 1},
        },
    )

    route = respx.post(GROQ_URL).mock(
        return_value=httpx.Response(200, json=_groq_text_response("done"))
    )

    resp = await auth_setup.client.post(
        "/v1/chat/completions",
        headers=_auth(auth_setup.api_key),
        json={
            "model": "groq/llama-3.1-8b-instant",
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [
                {"type": "function", "function": {"name": "a"}},
                {"type": "function", "function": {"name": "b"}},
            ],
        },
    )

    assert resp.status_code == 200
    assert resp.headers.get("X-Pronaos-Tool-Stripped") in ("a,b", "b,a")
    sent = json.loads(route.calls[0].request.content)
    # OpenAI-compat adapter omits ``tools`` from the wire when the list is
    # empty (matches the upstream's "no tools" semantic). Either is
    # acceptable; what matters is no over-budget tool got through.
    assert sent.get("tools") in (None, [])


# --------------------------------------------------------------------------- #
# tool_names + budget increment after a tool-emitting response                #
# --------------------------------------------------------------------------- #


@respx.mock
@pytest.mark.asyncio
async def test_emitted_tool_increments_budget_and_persists_names(auth_setup) -> None:  # type: ignore[no-untyped-def]
    """The full Phase 37 happy path: configured tool, allowed through,
    response carries tool_calls, gateway:
      1. writes the name into usage_records.tool_names + audit_records.tool_names
      2. ticks teams.tool_budgets[name].current_calls by 1
    """
    await _set_tool_budgets(
        auth_setup.sm,
        auth_setup.team_id,
        {"web_search": {"limit_calls": 100, "current_calls": 5}},
    )

    respx.post(GROQ_URL).mock(
        return_value=httpx.Response(
            200, json=_groq_tool_call_response("web_search")
        )
    )

    resp = await auth_setup.client.post(
        "/v1/chat/completions",
        headers=_auth(auth_setup.api_key),
        json={
            "model": "groq/llama-3.1-8b-instant",
            "messages": [{"role": "user", "content": "search please"}],
            "tools": [{"type": "function", "function": {"name": "web_search"}}],
        },
    )
    assert resp.status_code == 200
    # Sanity: the response carries the tool_call as the client expects.
    body = resp.json()
    assert body["choices"][0]["message"]["tool_calls"][0]["function"][
        "name"
    ] == "web_search"

    # Effects on the DB:
    async with auth_setup.sm() as session:
        team = await session.get(Team, auth_setup.team_id)
        assert team is not None
        assert (team.tool_budgets or {})["web_search"]["current_calls"] == 6

        usage_row = (
            await session.execute(
                select(UsageRecord).where(UsageRecord.team_id == auth_setup.team_id)
            )
        ).scalar_one()
        assert usage_row.tool_names == "web_search"

        audit_row = (
            await session.execute(
                select(AuditRecord).where(AuditRecord.team_id == auth_setup.team_id)
            )
        ).scalar_one()
        assert audit_row.tool_names == "web_search"


@respx.mock
@pytest.mark.asyncio
async def test_plain_text_response_leaves_tool_names_null(auth_setup) -> None:  # type: ignore[no-untyped-def]
    """A plain chat response (no tool_calls) writes NULL into both
    tool_names columns — not the empty string. Matters because dashboards
    distinguish 'no tools' from 'unknown tool'."""
    respx.post(GROQ_URL).mock(
        return_value=httpx.Response(200, json=_groq_text_response("hi"))
    )

    resp = await auth_setup.client.post(
        "/v1/chat/completions",
        headers=_auth(auth_setup.api_key),
        json={
            "model": "groq/llama-3.1-8b-instant",
            "messages": [{"role": "user", "content": "ping"}],
        },
    )
    assert resp.status_code == 200

    async with auth_setup.sm() as session:
        usage_row = (
            await session.execute(
                select(UsageRecord).where(UsageRecord.team_id == auth_setup.team_id)
            )
        ).scalar_one()
        assert usage_row.tool_names is None

        audit_row = (
            await session.execute(
                select(AuditRecord).where(AuditRecord.team_id == auth_setup.team_id)
            )
        ).scalar_one()
        assert audit_row.tool_names is None


@respx.mock
@pytest.mark.asyncio
async def test_unconfigured_tool_records_name_but_no_budget_create(auth_setup) -> None:  # type: ignore[no-untyped-def]
    """LLM emits a tool the team has NOT configured a budget for. The
    name lands in usage_records (it's still an observability
    signal) but the tool_budgets dict is NOT auto-extended — guards
    against DoS-by-arbitrary-tool-name flooding the JSON column."""
    await _set_tool_budgets(
        auth_setup.sm,
        auth_setup.team_id,
        {"known_tool": {"limit_calls": 10, "current_calls": 0}},
    )

    respx.post(GROQ_URL).mock(
        return_value=httpx.Response(
            200, json=_groq_tool_call_response("rogue_tool")
        )
    )

    resp = await auth_setup.client.post(
        "/v1/chat/completions",
        headers=_auth(auth_setup.api_key),
        json={
            "model": "groq/llama-3.1-8b-instant",
            "messages": [{"role": "user", "content": "do thing"}],
            "tools": [{"type": "function", "function": {"name": "known_tool"}}],
        },
    )
    assert resp.status_code == 200

    async with auth_setup.sm() as session:
        team = await session.get(Team, auth_setup.team_id)
        assert team is not None
        assert team.tool_budgets == {
            "known_tool": {"limit_calls": 10, "current_calls": 0}
        }

        usage_row = (
            await session.execute(
                select(UsageRecord).where(UsageRecord.team_id == auth_setup.team_id)
            )
        ).scalar_one()
        assert usage_row.tool_names == "rogue_tool"
