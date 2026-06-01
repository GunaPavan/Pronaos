"""Unit tests for GET /v1/admin/team/{id}/ab-test (Phase 66 gap fill).

Covers:
- Returns 200 with stats and t-test when the team has an active test + usage rows.
- Returns 403 on a chat:write-only key.
- Returns 404 when the team_id is not found.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import pytest_asyncio

from pronaos.auth.api_keys import generate_api_key, hash_key
from pronaos.config import get_settings
from pronaos.core.quota import QuotaTracker
from pronaos.core.ratelimit import InMemoryRateLimiter
from pronaos.core.router import Router
from pronaos.db.models import ApiKey, Base, Team, Tenant, UsageRecord
from pronaos.db.session import create_engine, create_sessionmaker
from pronaos.main import create_app
from pronaos.providers.registry import ProviderRegistry


@pytest_asyncio.fixture
async def setup(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[dict[str, Any]]:
    monkeypatch.setenv("PRONAOS_DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    get_settings.cache_clear()
    settings = get_settings()

    engine = create_engine(settings)
    sm = create_sessionmaker(engine)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    full_admin, prefix_admin = generate_api_key("test")
    full_chat, prefix_chat = generate_api_key("test")

    ab_test_blob: dict[str, Any] = {
        "id": "test-uuid-001",
        "name": "Haiku vs Sonnet",
        "started_at": "2026-01-01T00:00:00",
        "arm_a": {"model": "anthropic/claude-haiku-4-5", "weight": 0.5},
        "arm_b": {"model": "anthropic/claude-sonnet-4-5", "weight": 0.5},
    }

    team_id: str
    tenant_id: str
    async with sm() as session:
        async with session.begin():
            ta = Tenant(name="abtest-tenant-a")
            session.add(ta)
            await session.flush()
            tenant_id = ta.id
            team = Team(tenant_id=ta.id, name="abtest-team-1", ab_test=ab_test_blob)
            session.add(team)
            await session.flush()
            team_id = team.id

            admin_key = ApiKey(
                team_id=team_id,
                prefix=prefix_admin,
                key_hash=hash_key(full_admin),
                scopes="admin:usage",
                label="admin-a",
            )
            session.add(admin_key)
            session.add(
                ApiKey(
                    team_id=team_id,
                    prefix=prefix_chat,
                    key_hash=hash_key(full_chat),
                    scopes="chat:write",
                    label="chat-only",
                )
            )
            await session.flush()
            admin_key_id = admin_key.id

            ts_base = datetime(2026, 1, 2, tzinfo=UTC)
            for i in range(5):
                session.add(
                    UsageRecord(
                        id=f"ur-arm-a-{i}",
                        tenant_id=tenant_id,
                        team_id=team_id,
                        key_id=admin_key_id,
                        model="anthropic/claude-haiku-4-5",
                        provider="anthropic",
                        prompt_tokens=100,
                        completion_tokens=50,
                        cost_hcents=40 + i,
                        ab_arm="a",
                        status="success",
                        ts=ts_base,
                    )
                )
                session.add(
                    UsageRecord(
                        id=f"ur-arm-b-{i}",
                        tenant_id=tenant_id,
                        team_id=team_id,
                        key_id=admin_key_id,
                        model="anthropic/claude-sonnet-4-5",
                        provider="anthropic",
                        prompt_tokens=100,
                        completion_tokens=50,
                        cost_hcents=180 + i * 2,
                        ab_arm="b",
                        status="success",
                        ts=ts_base,
                    )
                )

    app = create_app()
    app.state.db_engine = engine
    app.state.db_sessionmaker = sm
    registry = ProviderRegistry(settings)
    app.state.provider_registry = registry
    app.state.router = Router(registry, default_provider=None)
    app.state.rate_limiter = InMemoryRateLimiter()
    app.state.quota_tracker = QuotaTracker()

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield {
                "client": client,
                "admin_key": full_admin,
                "chat_key": full_chat,
                "team_id": team_id,
            }
    finally:
        await registry.aclose()
        await engine.dispose()
        get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# Tests                                                                        #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_ab_test_with_active_test_and_usage(setup: dict[str, Any]) -> None:
    """Returns config + arm stats + t-test when the team has rows in both arms."""
    client: httpx.AsyncClient = setup["client"]
    r = await client.get(
        f"/v1/admin/team/{setup['team_id']}/ab-test",
        headers={"Authorization": f"Bearer {setup['admin_key']}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["test_id"] == "test-uuid-001"
    assert body["test_name"] == "Haiku vs Sonnet"
    assert body["arm_a_model"] == "anthropic/claude-haiku-4-5"
    assert body["arm_b_model"] == "anthropic/claude-sonnet-4-5"

    assert body["arm_a_stats"]["n"] == 5
    assert body["arm_b_stats"]["n"] == 5

    # Arm A mean: (40+41+42+43+44)/5 = 42.0
    assert abs(body["arm_a_stats"]["mean_cost_hcents"] - 42.0) < 0.1
    # Arm B mean: (180+182+184+186+188)/5 = 184.0
    assert abs(body["arm_b_stats"]["mean_cost_hcents"] - 184.0) < 0.1

    # t-test fires and is significant (huge cost difference, small variance).
    assert body["t_test"] is not None
    assert body["t_test"]["significant_at_05"] is True
    assert body["t_test"]["p_value"] < 0.05


@pytest.mark.asyncio
async def test_ab_test_404_for_unknown_team(setup: dict[str, Any]) -> None:
    """Non-existent team_id → 404."""
    client: httpx.AsyncClient = setup["client"]
    r = await client.get(
        "/v1/admin/team/nonexistent_team_xyz/ab-test",
        headers={"Authorization": f"Bearer {setup['admin_key']}"},
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_ab_test_missing_scope(setup: dict[str, Any]) -> None:
    """chat:write key → 403."""
    client: httpx.AsyncClient = setup["client"]
    r = await client.get(
        f"/v1/admin/team/{setup['team_id']}/ab-test",
        headers={"Authorization": f"Bearer {setup['chat_key']}"},
    )
    assert r.status_code == 403
