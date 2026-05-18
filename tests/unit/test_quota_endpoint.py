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
    monthly_cost_hcents_budget: int | None = None,
    current_cost_hcents: int = 0,
) -> tuple[_Quota, async_sessionmaker]:
    """Stand up an in-memory SQLite + seeded tenant/team/key + FastAPI app
    with our quota stack. Returns the test client plus IDs.

    Cost-budget knobs are off by default so existing tests that only care
    about token budgets behave exactly as before Phase 5.7."""
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
            monthly_cost_hcents_budget=monthly_cost_hcents_budget,
            current_period_cost_hcents=current_cost_hcents,
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
        # Phase 5.7 made the reason code specific. ``monthly_budget_exhausted``
        # was renamed to ``monthly_token_budget_exhausted`` so cost-budget
        # denials can carry their own distinct code.
        assert detail["type"] == "monthly_token_budget_exhausted"
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
        # Phase 20: set max_tokens explicitly so the preflight estimator
        # doesn't default to 4096 (which would trip the 100-token budget
        # even AFTER rollover). This test is about period rollover, not
        # preflight — keep the two concerns orthogonal.
        "max_tokens": 10,
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


# --------------------------------------------------------------------------- #
# Phase 5.7 — cost-budget gate                                                 #
# --------------------------------------------------------------------------- #


@respx.mock
@pytest.mark.asyncio
async def test_cost_budget_exhausted_returns_429(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Team has a $10 (=10,000 hcents) cost budget already fully spent;
    next call → 429 with the cost-specific reason code so dashboards can
    tell token-exhaustion apart from cost-exhaustion."""
    q, _ = await _build_test_app(
        monkeypatch,
        rps_limit=None,
        monthly_budget=None,
        monthly_cost_hcents_budget=10_000,
        current_cost_hcents=10_000,
    )
    respx.post(GROQ_URL).mock(return_value=httpx.Response(200, json=_groq_response()))

    headers = {"Authorization": f"Bearer {q.api_key}"}
    body = {
        "model": "groq/llama-3.1-8b-instant",
        "messages": [{"role": "user", "content": "x"}],
    }
    try:
        r = await q.client.post("/v1/chat/completions", headers=headers, json=body)
        assert r.status_code == 429, r.text
        detail = r.json().get("detail")
        assert isinstance(detail, dict)
        assert detail["type"] == "monthly_cost_budget_exhausted"
    finally:
        await q.client.aclose()


@respx.mock
@pytest.mark.asyncio
async def test_cost_counter_increments_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After a successful call, current_period_cost_hcents should bump by the
    provider's computed cost. We don't pin the exact cost (the pricing map
    can change) — we just assert it's nonzero and strictly greater than the
    initial seed value.

    Tokens are deliberately large so the cost survives integer division at
    Groq's tier (5K hcents/Mtok input + 8K/Mtok output → 5K tokens each =
    25 + 40 = 65 hcents). At 10 tokens it'd round to 0 and the increment
    would look broken even when it isn't."""
    q, sm = await _build_test_app(
        monkeypatch,
        rps_limit=None,
        monthly_budget=None,
        monthly_cost_hcents_budget=1_000_000,  # $100 — plenty of headroom
        current_cost_hcents=0,
    )
    respx.post(GROQ_URL).mock(
        return_value=httpx.Response(200, json=_groq_response(in_tokens=5_000, out_tokens=5_000))
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
        assert team.current_period_cost_hcents > 0, (
            "cost counter must increment after a successful call"
        )


@respx.mock
@pytest.mark.asyncio
async def test_cost_period_rollover_resets_counter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Past period_resets_at + non-zero cost_used → both counters reset on
    the next call. Token-counter rollover already tested; this confirms the
    new cost counter rolls in lockstep."""
    past = datetime.now(tz=UTC) - timedelta(days=1)
    q, sm = await _build_test_app(
        monkeypatch,
        rps_limit=None,
        monthly_budget=None,
        period_resets_at=past,
        monthly_cost_hcents_budget=10_000,
        current_cost_hcents=9_999,  # would block without rollover
    )
    respx.post(GROQ_URL).mock(
        return_value=httpx.Response(200, json=_groq_response(in_tokens=1, out_tokens=1))
    )

    headers = {"Authorization": f"Bearer {q.api_key}"}
    body = {
        "model": "groq/llama-3.1-8b-instant",
        "messages": [{"role": "user", "content": "x"}],
    }
    try:
        r = await q.client.post("/v1/chat/completions", headers=headers, json=body)
        # If rollover hadn't fired, the seeded 9,999 hcents would have
        # triggered the budget-exhausted denial. Receiving 200 proves the
        # cost counter was zeroed at the boundary.
        assert r.status_code == 200, r.text
    finally:
        await q.client.aclose()

    async with sm() as session:
        team = await session.get(Team, q.team_id)
        assert team is not None
        # After rollover + the single call: counter should reflect ONLY
        # this call's cost, not the pre-rollover 9,999.
        assert team.current_period_cost_hcents < 9_999, (
            "cost counter must reset on rollover"
        )


# --------------------------------------------------------------------------- #
# Phase 6.1 — quota denials show up in Prometheus                             #
# --------------------------------------------------------------------------- #


@respx.mock
@pytest.mark.asyncio
async def test_quota_denial_increments_metric(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 429 from cost-budget exhaustion must bump
    pronaos_quota_denials_total{reason="monthly_cost_budget_exhausted"} —
    that's the metric a dashboard needs to alert on 'tenants hitting their
    cap' without parsing log lines."""
    from pronaos.observability.metrics import quota_denials_total

    def value(reason: str) -> float:
        try:
            return quota_denials_total.labels(reason=reason)._value.get()
        except KeyError:
            return 0.0

    q, _ = await _build_test_app(
        monkeypatch,
        rps_limit=None,
        monthly_budget=None,
        monthly_cost_hcents_budget=10_000,
        current_cost_hcents=10_000,
    )
    respx.post(GROQ_URL).mock(return_value=httpx.Response(200, json=_groq_response()))

    before = value("monthly_cost_budget_exhausted")
    headers = {"Authorization": f"Bearer {q.api_key}"}
    body = {
        "model": "groq/llama-3.1-8b-instant",
        "messages": [{"role": "user", "content": "x"}],
    }
    try:
        r = await q.client.post("/v1/chat/completions", headers=headers, json=body)
        assert r.status_code == 429
    finally:
        await q.client.aclose()

    after = value("monthly_cost_budget_exhausted")
    assert after - before == pytest.approx(1.0)


@respx.mock
@pytest.mark.asyncio
async def test_cost_denial_supersedes_token_headroom(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tokens have plenty of headroom (10,000 budget, 0 used) but cost is
    exhausted. The combined gate must still deny — cost is checked first
    precisely so an expensive-provider request can't slip through on
    cheap-provider token headroom."""
    q, _ = await _build_test_app(
        monkeypatch,
        rps_limit=None,
        monthly_budget=10_000,
        current_tokens=0,
        monthly_cost_hcents_budget=1_000,
        current_cost_hcents=1_000,
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
        assert r.json()["detail"]["type"] == "monthly_cost_budget_exhausted"
    finally:
        await q.client.aclose()
