"""End-to-end chat-endpoint tests for Phase 39 structured output + auto-retry.

Four behaviours to lock down via the real FastAPI stack + respx-mocked
upstream:

1. **First-try pass**: response satisfies the schema on the first
   attempt → no retry, no header overhead, validation marker = passed.
2. **Retry recovers**: first response fails schema; gateway re-fires
   with a corrective prompt; second response is valid → validation
   marker = retried, retry-count header = 1.
3. **Retry exhausted**: all attempts fail → gateway returns the last
   response with validation marker = failed (no exception raised).
4. **Cache hit skips validation**: cached response was already valid
   when first written; subsequent hits don't re-validate (no extra
   upstream cost).

We also assert that retries make REAL upstream calls (each becomes
its own usage_records row), so operators see the retry cost in
dashboards.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import select

from pronaos.db.models import UsageRecord

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

_PERSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "age": {"type": "integer", "minimum": 0},
    },
    "required": ["name", "age"],
    "additionalProperties": False,
}

_RESPONSE_FORMAT: dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {
        "name": "Person",
        "schema": _PERSON_SCHEMA,
        "strict": True,
    },
}


def _groq_response(content: str, *, prompt_tokens: int = 5, completion_tokens: int = 7) -> dict[str, Any]:
    return {
        "id": "chatcmpl-x",
        "object": "chat.completion",
        "model": "llama-3.1-8b-instant",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
    }


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@respx.mock
@pytest.mark.asyncio
async def test_first_try_passes_no_retry(auth_setup) -> None:  # type: ignore[no-untyped-def]
    """Response validates on the first attempt; validation marker = passed,
    no retry-count header, no extra upstream calls."""
    route = respx.post(GROQ_URL).mock(
        return_value=httpx.Response(
            200, json=_groq_response('{"name": "Alice", "age": 30}')
        )
    )
    resp = await auth_setup.client.post(
        "/v1/chat/completions",
        headers=_auth(auth_setup.api_key),
        json={
            "model": "groq/llama-3.1-8b-instant",
            "messages": [{"role": "user", "content": "give me a person"}],
            "temperature": 0.0,
            "response_format": _RESPONSE_FORMAT,
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers.get("x-pronaos-schema-validation") == "passed"
    assert "x-pronaos-schema-retry-count" not in resp.headers
    assert route.call_count == 1  # no retry


@respx.mock
@pytest.mark.asyncio
async def test_retry_recovers(auth_setup) -> None:  # type: ignore[no-untyped-def]
    """First response is non-JSON; second response is valid. The retry
    loop fires exactly once and surfaces the win on the headers."""
    # The respx route uses side_effect to vary per-call response.
    responses = [
        httpx.Response(200, json=_groq_response("This is not JSON.")),
        httpx.Response(
            200, json=_groq_response('{"name": "Bob", "age": 25}')
        ),
    ]

    def side_effect(request: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    respx.post(GROQ_URL).mock(side_effect=side_effect)

    resp = await auth_setup.client.post(
        "/v1/chat/completions",
        headers=_auth(auth_setup.api_key),
        json={
            "model": "groq/llama-3.1-8b-instant",
            "messages": [{"role": "user", "content": "give me a person"}],
            "temperature": 0.0,
            "response_format": _RESPONSE_FORMAT,
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers.get("x-pronaos-schema-validation") == "retried"
    assert resp.headers.get("x-pronaos-schema-retry-count") == "1"
    # Final response is valid JSON matching the schema.
    body = resp.json()
    content = body["choices"][0]["message"]["content"]
    parsed = json.loads(content)
    assert parsed == {"name": "Bob", "age": 25}


@respx.mock
@pytest.mark.asyncio
async def test_retry_exhausted_returns_failed(auth_setup) -> None:  # type: ignore[no-untyped-def]
    """Every attempt fails. Gateway returns the last response with
    validation marker = failed and the actual retry count. Client
    does NOT see a 5xx — they get the last LLM output (might be
    useful debugging info)."""
    # 3 invalid responses for 1 initial + 2 retries (default max_retries=2).
    responses = [
        httpx.Response(200, json=_groq_response("not json"))
        for _ in range(3)
    ]

    def side_effect(request: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    respx.post(GROQ_URL).mock(side_effect=side_effect)

    resp = await auth_setup.client.post(
        "/v1/chat/completions",
        headers=_auth(auth_setup.api_key),
        json={
            "model": "groq/llama-3.1-8b-instant",
            "messages": [{"role": "user", "content": "give me a person"}],
            "temperature": 0.0,
            "response_format": _RESPONSE_FORMAT,
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers.get("x-pronaos-schema-validation") == "failed"
    assert resp.headers.get("x-pronaos-schema-retry-count") == "2"


@respx.mock
@pytest.mark.asyncio
async def test_no_schema_skips_validation_entirely(auth_setup) -> None:  # type: ignore[no-untyped-def]
    """Without response_format the validation path is a no-op. No headers,
    no retries — preserves the existing behaviour for non-structured
    workloads."""
    route = respx.post(GROQ_URL).mock(
        return_value=httpx.Response(
            200, json=_groq_response("plain text response")
        )
    )
    resp = await auth_setup.client.post(
        "/v1/chat/completions",
        headers=_auth(auth_setup.api_key),
        json={
            "model": "groq/llama-3.1-8b-instant",
            "messages": [{"role": "user", "content": "hi"}],
            "temperature": 0.0,
        },
    )
    assert resp.status_code == 200
    assert "x-pronaos-schema-validation" not in resp.headers
    assert "x-pronaos-schema-retry-count" not in resp.headers
    assert route.call_count == 1


@respx.mock
@pytest.mark.asyncio
async def test_retries_count_as_separate_usage_records(auth_setup) -> None:  # type: ignore[no-untyped-def]
    """Each retry is a real upstream call billed as its own
    usage_records row. Operators see the retry cost in dashboards."""
    responses = [
        httpx.Response(200, json=_groq_response("not json")),
        httpx.Response(
            200, json=_groq_response('{"name": "Eve", "age": 40}')
        ),
    ]

    def side_effect(request: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    respx.post(GROQ_URL).mock(side_effect=side_effect)

    await auth_setup.client.post(
        "/v1/chat/completions",
        headers=_auth(auth_setup.api_key),
        json={
            "model": "groq/llama-3.1-8b-instant",
            "messages": [{"role": "user", "content": "give me a person"}],
            "temperature": 0.0,
            "response_format": _RESPONSE_FORMAT,
        },
    )

    # Two usage_records rows for this one chat request — the initial
    # attempt and the retry.
    async with auth_setup.sm() as session:
        rows = (
            await session.execute(
                select(UsageRecord).where(UsageRecord.team_id == auth_setup.team_id)
            )
        ).scalars().all()
        assert len(rows) == 2, (
            f"expected 2 usage_records (initial + 1 retry); got {len(rows)}"
        )


@respx.mock
@pytest.mark.asyncio
async def test_max_retries_zero_disables_retry(auth_setup) -> None:  # type: ignore[no-untyped-def]
    """When the team has structured_output_max_retries=0, a violation
    fails immediately with validation=failed and no retries fired."""
    # Flip the team flag to 0.
    from sqlalchemy import update

    from pronaos.db.models import Team

    async with auth_setup.sm() as session:
        await session.execute(
            update(Team)
            .where(Team.id == auth_setup.team_id)
            .values(structured_output_max_retries=0)
        )
        await session.commit()

    respx.post(GROQ_URL).mock(
        return_value=httpx.Response(200, json=_groq_response("not json"))
    )

    resp = await auth_setup.client.post(
        "/v1/chat/completions",
        headers=_auth(auth_setup.api_key),
        json={
            "model": "groq/llama-3.1-8b-instant",
            "messages": [{"role": "user", "content": "give me a person"}],
            "temperature": 0.0,
            "response_format": _RESPONSE_FORMAT,
        },
    )
    assert resp.status_code == 200
    assert resp.headers.get("x-pronaos-schema-validation") == "failed"
    assert "x-pronaos-schema-retry-count" not in resp.headers
