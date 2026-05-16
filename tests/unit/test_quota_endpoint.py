"""HTTP-level integration tests for the Phase-4 quota gates.

These tests prove the *composition* of the limiter + tracker + middleware
chain through the real FastAPI app — burst → 429, budget exhaustion → 429,
unlimited keys/teams pass through, Retry-After header is set correctly.

We use ``client_with_registry`` with a manually-seeded DB so each test has
isolated state.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from pronaos.auth.api_keys import generate_api_key, hash_key
from pronaos.config import get_settings
from pronaos.core.quota import QuotaTracker
from pronaos.core.ratelimit import InMemoryRateLimiter
from pronaos.core.router import Router
from pronaos.db.models import ApiKey, Base, Team, Tenant
from pronaos.main import create_app
from pronaos.providers.registry import ProviderRegistry

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"


def _groq_response(content: str = "ok", in_tokens: int = 5, out_tokens: int = 3) -> dict:
    return {
        "id": "chatcmpl-x",
        "object": "chat.completion",
        "created": 1,
        "model": "llama-3.1-8b-instant",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": in_tokens,
            "completion_tokens": out_tokens,
            "total_tokens": in_tokens + out_tokens,
        },
    }


# --------------------------------------------------------------------------- #
# Test app fixture                                                            #
# --------------------------------------------------------------------------- #


class _Quota:
    """All state a quota test needs to interact with."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        api_key: str,
        team_id: str,
        key_id: str,
    ) -> None:
        self.client = client
        self.api_key = api_key
        self.team_id = team_id
        self.key_id = key_id


async def _build_test_app(
    monkeypatch: pytest.MonkeyPatch,
    *,
    rps_limit: int | None,
    monthly_budget: int | None,
    current_tokens: int = 0,
    period_resets_at: datetime | None = None,
) -> tuple[_Quota, async_sessionmaker]:
    """Stand up an in-memory SQLite + seeded tenant/team/key + FastAPI app
    with our quota stack. Returns the test client plus IDs."""
    monkeypatch.setenv("PRONAOS_DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    get_settings.cache_clear()
    settings = get_settings()

    engine = create_async_engine(settings.database_url, connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)

    # Seed tenant + team + key with the requested quota config
    full, prefix = generate_api_key("test")
    async with sm() as session:
        tenant = Tenant(name="acme-q")
        session.add(tenant)
        await session.flush()

        team = Team(
            tenant_id=tenant.id,
            name="eng-q",
            monthly_token_budget=monthly_budget,
            current_period_tokens=current_tokens,
        )
        if period_resets_at is not None:
            team.period_resets_at = period_resets_at
        session.add(team)
        await session.flush()

        key = ApiKey(
            team_id=team.id,
            prefix=prefix,
            key_hash=hash_key(full),
            scopes="chat:write",
            rps_limit=rps_limit,
        )
        session.add(key)
        await session.commit()
        team_id, key_id = team.id, key.id

    # Build app
    app = create_app()
    app.state.db_engine = engine
    app.state.db_sessionmaker = sm
    registry = ProviderRegistry(settings)
    app.state.provider_registry = registry
    app.state.router = Router(registry, default_provider=None)
    app.state.rate_limiter = InMemoryRateLimiter()
    app.state.quota_tracker = QuotaTracker()

    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://test")
    return _Quota(client, full, team_id, key_id), sm


# --------------------------------------------------------------------------- #
# RPS gate                                                                    #
# --------------------------------------------------------------------------- #


@respx.mock
@pytest.mark.asyncio
async def test_burst_within_rps_succeeds_then_denies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Key limited to 3 r/s. Twenty concurrent calls → exactly 3 succeed, 17
    get 429. Concurrent gather is the right way to test burst because
    sequential calls let the bucket refill between requests."""
    import asyncio

    q, _ = await _build_test_app(monkeypatch, rps_limit=3, monthly_budget=None)
    respx.post(GROQ_URL).mock(return_value=httpx.Response(200, json=_groq_response()))

    headers = {"Authorization": f"Bearer {q.api_key}"}
    body = {
        "model": "groq/llama-3.1-8b-instant",
        "messages": [{"role": "user", "content": "ping"}],
    }

    async def one_call() -> httpx.Response:
        return await q.client.post("/v1/chat/completions", headers=headers, json=body)

    try:
        results = await asyncio.gather(*[one_call() for _ in range(20)])
        statuses = [r.status_code for r in results]
        allowed = sum(1 for s in statuses if s == 200)
        denied = sum(1 for s in statuses if s == 429)
        assert allowed == 3, f"expected exactly 3 allowed, got {allowed} (statuses={statuses})"
        assert denied == 17

        # Any 429 response carries a Retry-After header and structured body
        denied_resp = next(r for r in results if r.status_code == 429)
        assert "retry-after" in {h.lower() for h in denied_resp.headers}
        assert int(denied_resp.headers["Retry-After"]) >= 1
        detail = denied_resp.json().get("detail")
        assert isinstance(detail, dict)
        assert detail["type"] == "rate_limit"
    finally:
        await q.client.aclose()


@respx.mock
@pytest.mark.asyncio
async def test_unlimited_rps_never_throttles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """rps_limit=None (the default) means no per-key throttling at all."""
    q, _ = await _build_test_app(monkeypatch, rps_limit=None, monthly_budget=None)
    respx.post(GROQ_URL).mock(return_value=httpx.Response(200, json=_groq_response()))

    headers = {"Authorization": f"Bearer {q.api_key}"}
    body = {
        "model": "groq/llama-3.1-8b-instant",
        "messages": [{"role": "user", "content": "x"}],
    }
    try:
        for _ in range(20):
            r = await q.client.post("/v1/chat/completions", headers=headers, json=body)
            assert r.status_code == 200, r.text
    finally:
        await q.client.aclose()


# --------------------------------------------------------------------------- #
# Token-budget gate                                                           #
# --------------------------------------------------------------------------- #


@respx.mock
@pytest.mark.asyncio
async def test_budget_exhausted_returns_429(monkeypatch: pytest.MonkeyPatch) -> None:
    """Team has a 100-token budget and already consumed 100; next call → 429."""
    q, _ = await _build_test_app(
        monkeypatch, rps_limit=None, monthly_budget=100, current_tokens=100
    )
    respx.post(GROQ_URL).mock(return_value=httpx.Response(200, json=_groq_response()))

    headers = {"Authorization": f"Bearer {q.api_key}"}
    body = {
        "model": "groq/llama-3.1-8b-instant",
        "messages": [{"role": "user", "content": "x"}],
    }
    try:
        r = await q.client.post("/v1/chat/completions", headers=headers, json=body)
        assert r.status_code == 429
        # Body identifies the specific reason — clients can tell rate-limit
        # apart from budget exhaustion.
        err = r.json()
        # FastAPI HTTPException.detail is the dict we returned
        detail = err.get("detail") if isinstance(err, dict) else None
        assert isinstance(detail, dict)
        assert detail["type"] == "monthly_budget_exhausted"
    finally:
        await q.client.aclose()


@respx.mock
@pytest.mark.asyncio
async def test_budget_records_actual_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After a successful call, current_period_tokens is incremented by the
    actual usage from the provider's response (5 + 3 = 8 in this test)."""
    q, sm = await _build_test_app(
        monkeypatch, rps_limit=None, monthly_budget=10_000, current_tokens=0
    )
    respx.post(GROQ_URL).mock(
        return_value=httpx.Response(200, json=_groq_response(in_tokens=5, out_tokens=3))
    )

    headers = {"Authorization": f"Bearer {q.api_key}"}
    body = {
        "model": "groq/llama-3.1-8b-instant",
        "messages": [{"role": "user", "content": "x"}],
    }
    try:
        r = await q.client.post("/v1/chat/completions", headers=headers, json=body)
        assert r.status_code == 200, r.text
    finally:
        await q.client.aclose()

    async with sm() as session:
        team = await session.get(Team, q.team_id)
        assert team is not None
        assert team.current_period_tokens == 8


@respx.mock
@pytest.mark.asyncio
async def test_period_rollover_resets_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A team whose period_resets_at is in the past should see its
    counter reset on the next request."""
    past = datetime.now(tz=UTC) - timedelta(days=1)
    q, sm = await _build_test_app(
        monkeypatch,
        rps_limit=None,
        monthly_budget=100,
        current_tokens=99,
        period_resets_at=past,
    )
    respx.post(GROQ_URL).mock(
        return_value=httpx.Response(200, json=_groq_response(in_tokens=2, out_tokens=2))
    )

    headers = {"Authorization": f"Bearer {q.api_key}"}
    body = {
        "model": "groq/llama-3.1-8b-instant",
        "messages": [{"role": "user", "content": "x"}],
    }
    try:
        r = await q.client.post("/v1/chat/completions", headers=headers, json=body)
        assert r.status_code == 200, r.text
    finally:
        await q.client.aclose()

    # After rollover + this call: counter should be 4 (not 103)
    async with sm() as session:
        team = await session.get(Team, q.team_id)
        assert team is not None
        assert team.current_period_tokens == 4
