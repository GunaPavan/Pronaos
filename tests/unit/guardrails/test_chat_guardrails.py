"""HTTP-level integration: guardrails fire inside the chat handler.

The engine itself is exercised in test_engine.py; these tests prove the
wiring — ingress redaction reaches the provider request, egress
redaction reaches the response body, the X-Pronaos-Guardrails header
reports verdicts, and cache keys are derived AFTER ingress redaction
(so cached responses can't leak PII via L1).
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
from pronaos.core.quota import QuotaTracker
from pronaos.core.ratelimit import InMemoryRateLimiter
from pronaos.core.router import Router
from pronaos.db.models import ApiKey, Base, Team, Tenant
from pronaos.db.session import create_engine, create_sessionmaker
from pronaos.guardrails.detectors import PromptInjectionDetector, default_pii_detectors
from pronaos.guardrails.engine import DefaultGuardrailEngine
from pronaos.main import create_app
from pronaos.providers.anthropic import ANTHROPIC_API_URL
from pronaos.providers.registry import ProviderRegistry


def _anthropic_response(text: str = "ok") -> dict:
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
# Fixture                                                                     #
# --------------------------------------------------------------------------- #


@pytest_asyncio.fixture
async def guardrails_setup(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Any]:
    """auth_setup-equivalent with a real guardrail engine + L1 cache wired."""
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
        tenant = Tenant(name="acme-g")
        session.add(tenant)
        await session.flush()
        team = Team(tenant_id=tenant.id, name="eng")
        session.add(team)
        await session.flush()
        key = ApiKey(
            team_id=team.id,
            prefix=prefix,
            key_hash=hash_key(full),
            scopes="chat:write",
            label="guard-test",
        )
        session.add(key)
        await session.commit()

    app = create_app()
    app.state.db_engine = engine
    app.state.db_sessionmaker = sm
    registry = ProviderRegistry(settings)
    app.state.provider_registry = registry
    app.state.router = Router(registry, default_provider=None)
    app.state.rate_limiter = InMemoryRateLimiter()
    app.state.quota_tracker = QuotaTracker()
    app.state.cache = RedisExactCache(fakeredis.aioredis.FakeRedis())
    app.state.guardrails = DefaultGuardrailEngine(
        rules=[*default_pii_detectors(), PromptInjectionDetector()]
    )

    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield type("Setup", (), {"client": c, "api_key": full})()
    finally:
        await registry.aclose()
        await app.state.cache.aclose()
        await engine.dispose()
        get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# Ingress: provider sees redacted prompt                                      #
# --------------------------------------------------------------------------- #


@respx.mock
@pytest.mark.asyncio
async def test_ingress_redaction_reaches_provider(guardrails_setup) -> None:  # type: ignore[no-untyped-def]
    """The whole point: when a user prompt has an email, the email
    must NOT appear in the body the provider receives. The redaction
    happened before the failover layer forwarded the call upstream."""
    route = respx.post(ANTHROPIC_API_URL).mock(
        return_value=httpx.Response(200, json=_anthropic_response())
    )

    r = await guardrails_setup.client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {guardrails_setup.api_key}"},
        json={
            "model": "anthropic/claude-opus-4-7",
            "messages": [{"role": "user", "content": "Email me at alice@example.com"}],
            "temperature": 0.0,
        },
    )
    assert r.status_code == 200, r.text

    # Inspect what was actually sent to Anthropic.
    sent_body = json.loads(route.calls[0].request.content)
    sent_user_message = sent_body["messages"][0]["content"]
    assert "alice@example.com" not in sent_user_message, (
        "raw email reached the provider — ingress guardrail failed"
    )
    assert "[REDACTED-EMAIL]" in sent_user_message

    # The response header signals what fired.
    assert r.headers.get("x-pronaos-guardrails", "").startswith("redacted:")
    assert "pii.email" in r.headers.get("x-pronaos-guardrails", "")


# --------------------------------------------------------------------------- #
# Egress: assistant PII leak-back is scrubbed before reaching the client      #
# --------------------------------------------------------------------------- #


@respx.mock
@pytest.mark.asyncio
async def test_egress_redaction_scrubs_response(guardrails_setup) -> None:  # type: ignore[no-untyped-def]
    """If the provider response contains PII (training-data regurgitation
    or model echoing back input), the egress scan must strip it before
    the client sees it."""
    # Mock the provider to return content with an SSN — simulating leakback.
    respx.post(ANTHROPIC_API_URL).mock(
        return_value=httpx.Response(
            200,
            json=_anthropic_response(text="His SSN is 123-45-6789, FYI"),
        )
    )

    r = await guardrails_setup.client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {guardrails_setup.api_key}"},
        json={
            "model": "anthropic/claude-opus-4-7",
            "messages": [{"role": "user", "content": "tell me a fact"}],
            "temperature": 0.0,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    content = body["choices"][0]["message"]["content"]
    assert "123-45-6789" not in content, "SSN leaked through egress"
    assert "[REDACTED-SSN]" in content


# --------------------------------------------------------------------------- #
# Cache + guardrails correctness coupling                                     #
# --------------------------------------------------------------------------- #


@respx.mock
@pytest.mark.asyncio
async def test_cache_key_uses_redacted_content(guardrails_setup) -> None:  # type: ignore[no-untyped-def]
    """Two requests that differ ONLY in the redacted PII must collide
    on the cache key — proves the cache derives keys POST-redaction.
    Without this property the cache would never hit for prompts that
    contain user-specific identifiers, defeating the purpose."""
    route = respx.post(ANTHROPIC_API_URL).mock(
        return_value=httpx.Response(200, json=_anthropic_response(text="server says ok"))
    )
    headers = {"Authorization": f"Bearer {guardrails_setup.api_key}"}

    # First request: alice's email.
    r1 = await guardrails_setup.client.post(
        "/v1/chat/completions",
        headers=headers,
        json={
            "model": "anthropic/claude-opus-4-7",
            "messages": [{"role": "user", "content": "Email me at alice@example.com"}],
            "temperature": 0.0,
        },
    )
    assert r1.status_code == 200

    # Second request: bob's email, otherwise identical. Both redact to
    # the same canonical form so the cache MUST hit.
    r2 = await guardrails_setup.client.post(
        "/v1/chat/completions",
        headers=headers,
        json={
            "model": "anthropic/claude-opus-4-7",
            "messages": [{"role": "user", "content": "Email me at bob@example.com"}],
            "temperature": 0.0,
        },
    )
    assert r2.status_code == 200
    assert route.call_count == 1, (
        "second request should have hit cache; instead a second provider call happened"
    )


# --------------------------------------------------------------------------- #
# BLOCK action — currently no rule defaults to BLOCK, so just verify the     #
# default policy doesn't 422 a clean request.                                 #
# --------------------------------------------------------------------------- #


@respx.mock
@pytest.mark.asyncio
async def test_clean_request_passes_through_unchanged(guardrails_setup) -> None:  # type: ignore[no-untyped-def]
    """A prompt with no PII or injection patterns must reach the
    provider unmodified. Defends against an over-eager regex
    accidentally redacting innocuous text."""
    route = respx.post(ANTHROPIC_API_URL).mock(
        return_value=httpx.Response(200, json=_anthropic_response())
    )

    r = await guardrails_setup.client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {guardrails_setup.api_key}"},
        json={
            "model": "anthropic/claude-opus-4-7",
            "messages": [{"role": "user", "content": "What is the capital of France?"}],
            "temperature": 0.0,
        },
    )
    assert r.status_code == 200
    sent_body = json.loads(route.calls[0].request.content)
    assert sent_body["messages"][0]["content"] == "What is the capital of France?"
    # No header should be set when nothing fired.
    assert "x-pronaos-guardrails" not in {h.lower() for h in r.headers}
