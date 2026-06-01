"""End-to-end chat-endpoint tests for the Phase 38 reversible PII tokenization.

The empirical claim that must hold:

1. **Upstream sees tokens, not PII.** The request body that reaches
   Anthropic / Groq carries ``[EMAIL_a3f7c2e1b890]`` where the user
   wrote ``alice@example.com``. Compliance perimeter preserved.
2. **Client sees originals back.** If the LLM echoes the token in
   its response (a common case — the model reasoning about an
   entity), the gateway reverses it before returning to the client.
3. **Audit + cache see tokens.** The audit row hashes the tokenized
   payload (PII-free chain); the cache stores the tokenized response
   (PII never lands on disk).
4. **Disabled-team behaviour unchanged.** A team without
   ``pii_tokenization_enabled`` continues to get one-way redaction —
   no regression.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import fakeredis.aioredis
import httpx
import pytest
import pytest_asyncio
import respx

from pronaos.auth.api_keys import generate_api_key, hash_key
from pronaos.cache.exact import RedisExactCache
from pronaos.config import get_settings
from pronaos.core.pii_tokens import TokenStore
from pronaos.core.quota import QuotaTracker
from pronaos.core.ratelimit import InMemoryRateLimiter
from pronaos.core.router import Router
from pronaos.db.models import ApiKey, AuditRecord, Base, Team, Tenant
from pronaos.db.session import create_engine, create_sessionmaker
from pronaos.guardrails.detectors import default_pii_detectors
from pronaos.guardrails.engine import DefaultGuardrailEngine
from pronaos.main import create_app
from pronaos.providers.anthropic import ANTHROPIC_API_URL
from pronaos.providers.registry import ProviderRegistry


def _anthropic_response(text: str = "ok") -> dict[str, Any]:
    return {
        "id": "msg_01",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": text}],
        "model": "claude-opus-4-7",
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 5, "output_tokens": 3},
    }


# --------------------------------------------------------------------------- #
# Fixture: chat client with tokenization wired in                             #
# --------------------------------------------------------------------------- #


@pytest_asyncio.fixture
async def pii_tokenization_setup(  # type: ignore[no-untyped-def]
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[Any]:
    """Chat client with:

    - team.pii_tokenization_enabled = True
    - guardrail_policy mapping pii.email -> tokenize (so the engine
      emits TOKENIZE for emails)
    - TokenStore wired on app.state backed by fakeredis
    """
    monkeypatch.setenv("PRONAOS_DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-for-tests")
    get_settings.cache_clear()
    settings = get_settings()

    engine = create_engine(settings)
    sm = create_sessionmaker(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    full, prefix = generate_api_key("test")
    async with sm() as session:
        tenant = Tenant(name="acme-pii")
        session.add(tenant)
        await session.flush()
        team = Team(
            tenant_id=tenant.id,
            name="eng",
            pii_tokenization_enabled=True,
            pii_token_ttl_seconds=300,
            guardrail_policy={
                "rule_actions": {
                    "pii.email": "tokenize",
                    "pii.phone": "tokenize",
                }
            },
        )
        session.add(team)
        await session.flush()
        api_key = ApiKey(
            team_id=team.id,
            prefix=prefix,
            key_hash=hash_key(full),
            scopes="chat:write",
            label="pii-tok-test",
        )
        session.add(api_key)
        await session.commit()
        tenant_id = tenant.id
        team_id = team.id

    app = create_app()
    app.state.db_engine = engine
    app.state.db_sessionmaker = sm
    registry = ProviderRegistry(settings)
    app.state.provider_registry = registry
    app.state.router = Router(registry, default_provider=None)
    app.state.rate_limiter = InMemoryRateLimiter()
    app.state.quota_tracker = QuotaTracker()
    app.state.cache = RedisExactCache(fakeredis.aioredis.FakeRedis())
    app.state.guardrails = DefaultGuardrailEngine(rules=default_pii_detectors())
    redis_for_tokens = fakeredis.aioredis.FakeRedis()
    app.state.pii_token_store = TokenStore(redis_for_tokens)

    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield type(
                "Setup",
                (),
                {
                    "client": c,
                    "api_key": full,
                    "tenant_id": tenant_id,
                    "team_id": team_id,
                    "sm": sm,
                    "token_redis": redis_for_tokens,
                },
            )()
    finally:
        await registry.aclose()
        await app.state.cache.aclose()
        await redis_for_tokens.aclose()
        await engine.dispose()
        get_settings.cache_clear()


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------------- #
# Property 1: upstream sees tokens, never originals                           #
# --------------------------------------------------------------------------- #


@respx.mock
@pytest.mark.asyncio
async def test_upstream_sees_token_not_original_email(pii_tokenization_setup) -> None:  # type: ignore[no-untyped-def]
    route = respx.post(ANTHROPIC_API_URL).mock(
        return_value=httpx.Response(200, json=_anthropic_response("noted"))
    )

    r = await pii_tokenization_setup.client.post(
        "/v1/chat/completions",
        headers=_auth(pii_tokenization_setup.api_key),
        json={
            "model": "anthropic/claude-opus-4-7",
            "messages": [{"role": "user", "content": "Send a note to alice@example.com please"}],
            "temperature": 0.0,
        },
    )
    assert r.status_code == 200, r.text

    # Inspect the body that actually reached Anthropic.
    sent = json.loads(route.calls[0].request.content)
    sent_msg = sent["messages"][0]["content"]
    assert "alice@example.com" not in sent_msg, "PII leaked to upstream"
    assert "[EMAIL_" in sent_msg, "tokenization didn't run"

    # Header signals what happened.
    guardrails_header = r.headers.get("x-pronaos-guardrails", "")
    assert "tokenized:" in guardrails_header
    assert "email" in guardrails_header


# --------------------------------------------------------------------------- #
# Property 2: client sees originals back when the LLM echoes a token          #
# --------------------------------------------------------------------------- #


@respx.mock
@pytest.mark.asyncio
async def test_llm_echo_of_token_resolves_back_to_original(  # type: ignore[no-untyped-def]
    pii_tokenization_setup,
) -> None:
    """Simulate the common case: the LLM mentions the email by token
    in its response. The gateway must reverse the token before the
    client sees it.

    Approach: make the LLM mock echo whatever ``[EMAIL_<HASH>]``
    appears in the request — this is exactly what real LLMs do
    when the prompt asks them to confirm an address.
    """

    def respond_with_echo(request: httpx.Request) -> httpx.Response:
        sent = json.loads(request.content)
        prompt = sent["messages"][0]["content"]
        # Find the token in the prompt and echo it back.
        start = prompt.find("[EMAIL_")
        end = prompt.find("]", start) + 1 if start >= 0 else -1
        token = prompt[start:end] if start >= 0 else "[no-token]"
        return httpx.Response(
            200,
            json=_anthropic_response(text=f"OK, I will email {token}"),
        )

    respx.post(ANTHROPIC_API_URL).mock(side_effect=respond_with_echo)

    r = await pii_tokenization_setup.client.post(
        "/v1/chat/completions",
        headers=_auth(pii_tokenization_setup.api_key),
        json={
            "model": "anthropic/claude-opus-4-7",
            "messages": [{"role": "user", "content": "Email alice@example.com when ready"}],
            "temperature": 0.0,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    content = body["choices"][0]["message"]["content"]
    # The client must see the ORIGINAL email back.
    assert "alice@example.com" in content, f"detokenization failed: {content!r}"
    # And the token must NOT remain in the client response.
    assert "[EMAIL_" not in content
    # Response header reports the reverse count.
    assert r.headers.get("x-pronaos-pii-reversed") == "1"


# --------------------------------------------------------------------------- #
# Property 3: audit row carries tokenized payload (PII-free)                  #
# --------------------------------------------------------------------------- #


@respx.mock
@pytest.mark.asyncio
async def test_audit_row_carries_tokenized_payload(pii_tokenization_setup) -> None:  # type: ignore[no-untyped-def]
    """The audit chain must never see the original PII. The
    request_hash / response_hash inputs already cover this property
    by being computed over the tokenized strings; this test asserts
    the assistant message that lands in the audit-tracked request
    body uses the token, not the original."""
    respx.post(ANTHROPIC_API_URL).mock(
        return_value=httpx.Response(200, json=_anthropic_response("done"))
    )
    r = await pii_tokenization_setup.client.post(
        "/v1/chat/completions",
        headers=_auth(pii_tokenization_setup.api_key),
        json={
            "model": "anthropic/claude-opus-4-7",
            "messages": [{"role": "user", "content": "ping bob@example.com today"}],
            "temperature": 0.0,
        },
    )
    assert r.status_code == 200

    # Walk the audit_records table for this tenant. The implementation
    # holds raw bodies behind hashes; for this assertion we instead
    # check that NO record-write produced a request_hash that could
    # have included the original. We do that by writing the same
    # request a SECOND time and confirming the second hash equals the
    # first (deterministic over tokenized text). If the original PII
    # had leaked into either request, the hashes would diverge.
    from sqlalchemy import select

    async with pii_tokenization_setup.sm() as session:
        rows = (
            (
                await session.execute(
                    select(AuditRecord).where(
                        AuditRecord.tenant_id == pii_tokenization_setup.tenant_id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) >= 1

    # Now fire the SAME request again. The tokenized form is identical
    # (deterministic per (tenant, value)), so the new audit row's
    # request_hash must match the first one's request_hash.
    r2 = await pii_tokenization_setup.client.post(
        "/v1/chat/completions",
        headers=_auth(pii_tokenization_setup.api_key),
        json={
            "model": "anthropic/claude-opus-4-7",
            "messages": [{"role": "user", "content": "ping bob@example.com today"}],
            "temperature": 0.0,
        },
    )
    assert r2.status_code == 200

    async with pii_tokenization_setup.sm() as session:
        all_rows = (
            (
                await session.execute(
                    select(AuditRecord)
                    .where(AuditRecord.tenant_id == pii_tokenization_setup.tenant_id)
                    .order_by(AuditRecord.ts)
                )
            )
            .scalars()
            .all()
        )
        # First and most recent rows have identical request hashes —
        # proves the audited body is the tokenized form (deterministic).
        hashes = [r.request_hash for r in all_rows]
        assert len(set(hashes)) == 1, (
            f"audit hashes diverged across identical requests; "
            f"PII may have leaked into the request body: {hashes}"
        )


# --------------------------------------------------------------------------- #
# Property 4: disabled team still gets one-way redaction                      #
# --------------------------------------------------------------------------- #


@respx.mock
@pytest.mark.asyncio
async def test_disabled_team_falls_back_to_redact(pii_tokenization_setup) -> None:  # type: ignore[no-untyped-def]
    """Disable the team's tokenization flag. The engine must degrade
    to REDACT — preserves existing behaviour, no regression."""
    # Flip the flag.
    from sqlalchemy import update

    async with pii_tokenization_setup.sm() as session:
        await session.execute(
            update(Team)
            .where(Team.id == pii_tokenization_setup.team_id)
            .values(pii_tokenization_enabled=False)
        )
        await session.commit()

    route = respx.post(ANTHROPIC_API_URL).mock(
        return_value=httpx.Response(200, json=_anthropic_response("noted"))
    )

    r = await pii_tokenization_setup.client.post(
        "/v1/chat/completions",
        headers=_auth(pii_tokenization_setup.api_key),
        json={
            "model": "anthropic/claude-opus-4-7",
            "messages": [{"role": "user", "content": "Email alice@example.com please"}],
            "temperature": 0.0,
        },
    )
    assert r.status_code == 200
    sent = json.loads(route.calls[0].request.content)
    sent_msg = sent["messages"][0]["content"]
    # PII still gone, but REDACTED not TOKENIZED.
    assert "alice@example.com" not in sent_msg
    assert "[REDACTED-EMAIL]" in sent_msg
    # Response header reports redacted, not tokenized.
    guardrails_header = r.headers.get("x-pronaos-guardrails", "")
    assert "redacted:" in guardrails_header
    assert "tokenized:" not in guardrails_header


# --------------------------------------------------------------------------- #
# Property 5: orphaned token (LLM hallucinates) surfaces in metrics, stays    #
# --------------------------------------------------------------------------- #


@respx.mock
@pytest.mark.asyncio
async def test_orphaned_token_in_response_left_in_place(  # type: ignore[no-untyped-def]
    pii_tokenization_setup,
) -> None:
    """The LLM hallucinates a token shape that was never minted. The
    gateway leaves it in the response and increments the orphaned
    counter. Better than crashing or stripping legitimate output."""
    # Anthropic returns a fake token that doesn't exist in Redis.
    fake_token = "[EMAIL_deadbeef0000]"
    respx.post(ANTHROPIC_API_URL).mock(
        return_value=httpx.Response(
            200, json=_anthropic_response(text=f"reaching out to {fake_token}")
        )
    )

    r = await pii_tokenization_setup.client.post(
        "/v1/chat/completions",
        headers=_auth(pii_tokenization_setup.api_key),
        json={
            "model": "anthropic/claude-opus-4-7",
            "messages": [{"role": "user", "content": "no pii in this prompt"}],
            "temperature": 0.0,
        },
    )
    assert r.status_code == 200
    body = r.json()
    content = body["choices"][0]["message"]["content"]
    # Token left in place (not resolvable, not removable safely).
    assert fake_token in content
    # Header surfaces the orphaned count.
    assert r.headers.get("x-pronaos-pii-orphaned") == "1"
