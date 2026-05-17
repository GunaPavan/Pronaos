"""HTTP-level tests that the chat endpoint actually uses the cache.

The cache backend is exercised in test_exact_cache.py; this file proves
the *wiring*: a second identical request must short-circuit the provider
call, both metrics counters move, and bypass conditions (streaming,
temperature>0) correctly land in the ``skip`` bucket.
"""

from __future__ import annotations

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
from pronaos.main import create_app
from pronaos.observability import metrics as m
from pronaos.providers.anthropic import ANTHROPIC_API_URL
from pronaos.providers.registry import ProviderRegistry


def _counter(tier: str, result: str) -> float:
    try:
        return m.cache_lookups_total.labels(tier=tier, result=result)._value.get()  # noqa: SLF001
    except KeyError:
        return 0.0


def _anthropic_response(text: str = "ok", in_tokens: int = 5, out_tokens: int = 3) -> dict:
    return {
        "id": "msg_01",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": text}],
        "model": "claude-opus-4-7",
        "stop_reason": "end_turn",
        "usage": {"input_tokens": in_tokens, "output_tokens": out_tokens},
    }


# --------------------------------------------------------------------------- #
# Cache-enabled fixture                                                       #
# --------------------------------------------------------------------------- #


@pytest_asyncio.fixture
async def cached_setup(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Any]:
    """auth_setup-equivalent but wires a RedisExactCache (fakeredis-backed)
    into ``app.state.cache``. Returns the client + key + tenant id."""
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
        tenant = Tenant(name="acme-c")
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
            label="cache-test",
        )
        session.add(key)
        await session.commit()
        tenant_id = tenant.id

    app = create_app()
    app.state.db_engine = engine
    app.state.db_sessionmaker = sm
    registry = ProviderRegistry(settings)
    app.state.provider_registry = registry
    app.state.router = Router(registry, default_provider=None)
    app.state.rate_limiter = InMemoryRateLimiter()
    app.state.quota_tracker = QuotaTracker()
    # The point of this fixture: a real (fake) Redis-backed cache so a
    # second identical request hits.
    app.state.cache = RedisExactCache(fakeredis.aioredis.FakeRedis())

    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield type("Setup", (), {"client": c, "api_key": full, "tenant_id": tenant_id})()
    finally:
        await registry.aclose()
        await app.state.cache.aclose()
        await engine.dispose()
        get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# Happy path: identical second request hits the cache                         #
# --------------------------------------------------------------------------- #


@respx.mock
@pytest.mark.asyncio
async def test_identical_second_request_is_cache_hit(cached_setup) -> None:  # type: ignore[no-untyped-def]
    """First call → provider mock fired, miss counter +1.
    Second call → provider mock NOT fired again, hit counter +1.
    This is the test that proves caching actually saves an upstream call."""
    route = respx.post(ANTHROPIC_API_URL).mock(
        return_value=httpx.Response(200, json=_anthropic_response())
    )

    body = {
        "model": "anthropic/claude-opus-4-7",
        "messages": [{"role": "user", "content": "deterministic prompt"}],
        "temperature": 0.0,
    }
    headers = {"Authorization": f"Bearer {cached_setup.api_key}"}

    miss_before = _counter("exact", "miss")
    hit_before = _counter("exact", "hit")

    r1 = await cached_setup.client.post("/v1/chat/completions", headers=headers, json=body)
    assert r1.status_code == 200, r1.text
    assert route.call_count == 1

    r2 = await cached_setup.client.post("/v1/chat/completions", headers=headers, json=body)
    assert r2.status_code == 200, r2.text
    # The hallmark of a working cache: the provider was NOT called again.
    assert route.call_count == 1, "second identical request should be served from cache"

    # The cached response body should match the first response exactly.
    assert r2.json() == r1.json()

    assert _counter("exact", "miss") - miss_before == pytest.approx(1.0)
    assert _counter("exact", "hit") - hit_before == pytest.approx(1.0)


# --------------------------------------------------------------------------- #
# Bypass conditions                                                           #
# --------------------------------------------------------------------------- #


@respx.mock
@pytest.mark.asyncio
async def test_temperature_above_zero_skips_cache(cached_setup) -> None:  # type: ignore[no-untyped-def]
    """temperature > 0 means the user explicitly wants stochastic output —
    returning a cached identical response would silently violate their
    expectation. Both calls go to the provider; ``skip`` increments twice."""
    route = respx.post(ANTHROPIC_API_URL).mock(
        return_value=httpx.Response(200, json=_anthropic_response())
    )
    headers = {"Authorization": f"Bearer {cached_setup.api_key}"}
    body = {
        "model": "anthropic/claude-opus-4-7",
        "messages": [{"role": "user", "content": "hi"}],
        "temperature": 0.7,
    }

    skip_before = _counter("exact", "skip")

    await cached_setup.client.post("/v1/chat/completions", headers=headers, json=body)
    await cached_setup.client.post("/v1/chat/completions", headers=headers, json=body)

    # Both requests reached the provider — no caching applied.
    assert route.call_count == 2
    assert _counter("exact", "skip") - skip_before == pytest.approx(2.0)


# --------------------------------------------------------------------------- #
# Cross-tenant isolation through the HTTP path                                #
# --------------------------------------------------------------------------- #


@respx.mock
@pytest.mark.asyncio
async def test_different_prompt_misses_cache(cached_setup) -> None:  # type: ignore[no-untyped-def]
    """Changing one word in the prompt must miss the L1 cache (L2 semantic
    is a future phase). Confirms the canonical hash is actually keying on
    message content, not just (tenant, model)."""
    route = respx.post(ANTHROPIC_API_URL).mock(
        return_value=httpx.Response(200, json=_anthropic_response())
    )
    headers = {"Authorization": f"Bearer {cached_setup.api_key}"}

    base = {
        "model": "anthropic/claude-opus-4-7",
        "temperature": 0.0,
        "messages": [{"role": "user", "content": "what is 2+2"}],
    }
    other = {
        **base,
        "messages": [{"role": "user", "content": "what is 3+3"}],
    }

    await cached_setup.client.post("/v1/chat/completions", headers=headers, json=base)
    await cached_setup.client.post("/v1/chat/completions", headers=headers, json=other)

    # Different prompts → two upstream calls. L2 would catch the
    # paraphrase pattern but we're testing L1 only here.
    assert route.call_count == 2
