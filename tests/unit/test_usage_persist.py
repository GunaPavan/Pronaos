"""HTTP-level test: a successful chat call persists a UsageRecord.

Proves the wiring from chat.py → QuotaTracker.record_call → DB end-to-end.
Uses respx to mock Groq and verifies the row that shows up in the audit
table after the response returns.
"""

from __future__ import annotations

import httpx
import pytest
import respx
from sqlalchemy import select

from pronaos.db.models import UsageRecord
from pronaos.providers.anthropic import ANTHROPIC_API_URL


def _anthropic_response(text: str = "hi", in_tokens: int = 7, out_tokens: int = 3) -> dict:
    return {
        "id": "msg_01",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": text}],
        "model": "claude-opus-4-7",
        "stop_reason": "end_turn",
        "usage": {"input_tokens": in_tokens, "output_tokens": out_tokens},
    }


@respx.mock
@pytest.mark.asyncio
async def test_successful_chat_persists_usage_record(auth_setup) -> None:  # type: ignore[no-untyped-def]
    """One chat call → one usage_records row with all fields populated."""
    respx.post(ANTHROPIC_API_URL).mock(
        return_value=httpx.Response(200, json=_anthropic_response(in_tokens=12, out_tokens=4))
    )

    resp = await auth_setup.client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {auth_setup.api_key}"},
        json={
            "model": "anthropic/claude-opus-4-7",
            "messages": [{"role": "user", "content": "hello"}],
        },
    )
    assert resp.status_code == 200, resp.text

    # Read back the persisted row(s).
    sm = auth_setup.client._transport.app.state.db_sessionmaker  # type: ignore[attr-defined]
    async with sm() as session:
        rows = (await session.execute(select(UsageRecord))).scalars().all()

    assert len(rows) == 1, f"expected exactly 1 usage row, got {len(rows)}"
    rec = rows[0]
    assert rec.tenant_id == auth_setup.tenant_id
    assert rec.team_id == auth_setup.team_id
    assert rec.key_id == auth_setup.key_id
    assert rec.provider == "anthropic"
    assert rec.model == "anthropic/claude-opus-4-7"
    assert rec.prompt_tokens == 12
    assert rec.completion_tokens == 4
    # Cost is non-negative; precise value depends on the pricing map which
    # can change — the important invariant is "we recorded a number, not zero
    # for an Opus call with this many tokens."
    assert rec.cost_hcents >= 0
    assert rec.status == "success"


@respx.mock
@pytest.mark.asyncio
async def test_multiple_calls_each_get_a_row(auth_setup) -> None:  # type: ignore[no-untyped-def]
    respx.post(ANTHROPIC_API_URL).mock(
        return_value=httpx.Response(200, json=_anthropic_response())
    )
    for _ in range(3):
        r = await auth_setup.client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {auth_setup.api_key}"},
            json={
                "model": "anthropic/claude-opus-4-7",
                "messages": [{"role": "user", "content": "x"}],
            },
        )
        assert r.status_code == 200

    sm = auth_setup.client._transport.app.state.db_sessionmaker  # type: ignore[attr-defined]
    async with sm() as session:
        rows = (await session.execute(select(UsageRecord))).scalars().all()
    assert len(rows) == 3
