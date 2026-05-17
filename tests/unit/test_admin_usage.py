"""HTTP-level tests for ``GET /v1/admin/usage`` (Phase 5.3).

Covers:
- Auth: missing/wrong scope → 401/403.
- Empty tenant → 200 with empty items and zeroed totals.
- Multiple rows → totals sum correctly across the whole filter set,
  not just the current page.
- Filters: time-range, team_id, provider, model, status all narrow the result.
- Tenant isolation: an admin key for tenant A cannot see tenant B's rows.
- Pagination: limit/offset work, ordering is ts DESC (newest first).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

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


# --------------------------------------------------------------------------- #
# Fixture                                                                     #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class AdminSetup:
    """Two tenants, each with one team. Tenant A has an admin:usage key and a
    chat:write-only key. Tenant B has only an admin:usage key — we use it to
    prove cross-tenant queries return zero rows.

    Seeded UsageRecord rows let tests assert filter behaviour without going
    through a real chat call (which would require respx mocking)."""

    client: httpx.AsyncClient
    admin_key_a: str
    chat_only_key_a: str
    admin_key_b: str
    tenant_a: str
    tenant_b: str
    team_a1: str
    team_a2: str


@pytest_asyncio.fixture
async def admin_setup(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AdminSetup]:
    monkeypatch.setenv("PRONAOS_DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-for-tests")
    get_settings.cache_clear()
    settings = get_settings()

    engine = create_engine(settings)
    sm = create_sessionmaker(engine)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    full_admin_a, prefix_admin_a = generate_api_key("test")
    full_chat_a, prefix_chat_a = generate_api_key("test")
    full_admin_b, prefix_admin_b = generate_api_key("test")

    async with sm() as session:
        # Tenant A — two teams so we can test team_id filtering
        ta = Tenant(name="acme-a")
        session.add(ta)
        await session.flush()
        team_a1 = Team(tenant_id=ta.id, name="eng-a1")
        team_a2 = Team(tenant_id=ta.id, name="eng-a2")
        session.add_all([team_a1, team_a2])
        await session.flush()

        admin_a = ApiKey(
            team_id=team_a1.id,
            prefix=prefix_admin_a,
            key_hash=hash_key(full_admin_a),
            scopes="admin:usage",
            label="admin-a",
        )
        chat_a = ApiKey(
            team_id=team_a1.id,
            prefix=prefix_chat_a,
            key_hash=hash_key(full_chat_a),
            scopes="chat:write",
            label="chat-a",
        )
        session.add_all([admin_a, chat_a])

        # Tenant B — one team, one admin key
        tb = Tenant(name="acme-b")
        session.add(tb)
        await session.flush()
        team_b1 = Team(tenant_id=tb.id, name="eng-b1")
        session.add(team_b1)
        await session.flush()
        admin_b = ApiKey(
            team_id=team_b1.id,
            prefix=prefix_admin_b,
            key_hash=hash_key(full_admin_b),
            scopes="admin:usage",
            label="admin-b",
        )
        session.add(admin_b)
        # Flush so admin_b.id and team_b1.id (and the earlier admin_a/chat_a
        # IDs) are populated before we reference them in UsageRecord rows
        # below. SQLAlchemy resolves ``default=_new_id`` at flush time, not
        # at model instantiation.
        await session.flush()

        # Seed usage. Five rows for tenant A (split across teams, providers,
        # statuses, days), one row for tenant B (isolation guard).
        now = datetime.now(tz=UTC)
        a_team1 = team_a1.id
        a_team2 = team_a2.id
        rows = [
            # team_a1, anthropic opus, success — 2 days ago
            UsageRecord(
                tenant_id=ta.id,
                team_id=a_team1,
                key_id=admin_a.id,
                provider="anthropic",
                model="anthropic/claude-opus-4-7",
                prompt_tokens=100,
                completion_tokens=50,
                cost_hcents=150,
                status="success",
                ts=now - timedelta(days=2),
            ),
            # team_a1, anthropic opus, success — 1 day ago
            UsageRecord(
                tenant_id=ta.id,
                team_id=a_team1,
                key_id=admin_a.id,
                provider="anthropic",
                model="anthropic/claude-opus-4-7",
                prompt_tokens=200,
                completion_tokens=100,
                cost_hcents=300,
                status="success",
                ts=now - timedelta(days=1),
            ),
            # team_a1, groq llama, success — 1 hour ago
            UsageRecord(
                tenant_id=ta.id,
                team_id=a_team1,
                key_id=admin_a.id,
                provider="groq",
                model="groq/llama-3.1-8b-instant",
                prompt_tokens=50,
                completion_tokens=25,
                cost_hcents=5,
                status="success",
                ts=now - timedelta(hours=1),
            ),
            # team_a2, anthropic opus, error — 30 min ago
            UsageRecord(
                tenant_id=ta.id,
                team_id=a_team2,
                key_id=admin_a.id,
                provider="anthropic",
                model="anthropic/claude-opus-4-7",
                prompt_tokens=10,
                completion_tokens=0,
                cost_hcents=10,
                status="error",
                ts=now - timedelta(minutes=30),
            ),
            # team_a2, anthropic haiku, success — 5 min ago (newest)
            UsageRecord(
                tenant_id=ta.id,
                team_id=a_team2,
                key_id=admin_a.id,
                provider="anthropic",
                model="anthropic/claude-haiku-3-5",
                prompt_tokens=20,
                completion_tokens=10,
                cost_hcents=2,
                status="success",
                ts=now - timedelta(minutes=5),
            ),
            # Tenant B — must never appear in tenant A's queries
            UsageRecord(
                tenant_id=tb.id,
                team_id=team_b1.id,
                key_id=admin_b.id,
                provider="anthropic",
                model="anthropic/claude-opus-4-7",
                prompt_tokens=9999,
                completion_tokens=9999,
                cost_hcents=9999,
                status="success",
                ts=now - timedelta(hours=2),
            ),
        ]
        session.add_all(rows)
        await session.commit()

        tenant_a_id, tenant_b_id = ta.id, tb.id
        team_a1_id, team_a2_id = a_team1, a_team2

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
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield AdminSetup(
                client=c,
                admin_key_a=full_admin_a,
                chat_only_key_a=full_chat_a,
                admin_key_b=full_admin_b,
                tenant_a=tenant_a_id,
                tenant_b=tenant_b_id,
                team_a1=team_a1_id,
                team_a2=team_a2_id,
            )
    finally:
        await registry.aclose()
        await engine.dispose()
        get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# Auth + scope                                                                #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_missing_auth_returns_401(admin_setup: AdminSetup) -> None:
    r = await admin_setup.client.get("/v1/admin/usage")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_chat_only_key_returns_403(admin_setup: AdminSetup) -> None:
    """A chat:write key without admin:usage must not be able to read usage —
    least-privilege guard."""
    r = await admin_setup.client.get(
        "/v1/admin/usage",
        headers={"Authorization": f"Bearer {admin_setup.chat_only_key_a}"},
    )
    assert r.status_code == 403
    assert "admin:usage" in r.text


# --------------------------------------------------------------------------- #
# Happy path — totals + items                                                 #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_unfiltered_query_returns_all_tenant_rows(admin_setup: AdminSetup) -> None:
    """Five seeded rows for tenant A, ordered newest first, with totals
    summed across the whole result set."""
    r = await admin_setup.client.get(
        "/v1/admin/usage",
        headers={"Authorization": f"Bearer {admin_setup.admin_key_a}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()

    assert len(body["items"]) == 5
    # Newest first
    timestamps = [item["ts"] for item in body["items"]]
    assert timestamps == sorted(timestamps, reverse=True)

    totals = body["totals"]
    assert totals["requests"] == 5
    assert totals["prompt_tokens"] == 100 + 200 + 50 + 10 + 20
    assert totals["completion_tokens"] == 50 + 100 + 25 + 0 + 10
    assert totals["total_tokens"] == totals["prompt_tokens"] + totals["completion_tokens"]
    assert totals["cost_hcents"] == 150 + 300 + 5 + 10 + 2


@pytest.mark.asyncio
async def test_empty_tenant_returns_zeroed_totals(admin_setup: AdminSetup) -> None:
    """Filter to a non-existent team — items empty, totals all zero. The
    aggregate query must use COALESCE so SUM-over-empty returns 0, not NULL."""
    r = await admin_setup.client.get(
        "/v1/admin/usage",
        params={"team_id": "nonexistent-team-id"},
        headers={"Authorization": f"Bearer {admin_setup.admin_key_a}"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["items"] == []
    assert body["totals"] == {
        "requests": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cost_hcents": 0,
    }


# --------------------------------------------------------------------------- #
# Filters                                                                     #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_filter_by_team_id(admin_setup: AdminSetup) -> None:
    r = await admin_setup.client.get(
        "/v1/admin/usage",
        params={"team_id": admin_setup.team_a2},
        headers={"Authorization": f"Bearer {admin_setup.admin_key_a}"},
    )
    body = r.json()
    # team_a2 has exactly 2 rows in the seed (error + haiku success)
    assert body["totals"]["requests"] == 2
    assert all(item["team_id"] == admin_setup.team_a2 for item in body["items"])


@pytest.mark.asyncio
async def test_filter_by_provider(admin_setup: AdminSetup) -> None:
    r = await admin_setup.client.get(
        "/v1/admin/usage",
        params={"provider": "groq"},
        headers={"Authorization": f"Bearer {admin_setup.admin_key_a}"},
    )
    body = r.json()
    assert body["totals"]["requests"] == 1
    assert body["items"][0]["provider"] == "groq"


@pytest.mark.asyncio
async def test_filter_by_model(admin_setup: AdminSetup) -> None:
    r = await admin_setup.client.get(
        "/v1/admin/usage",
        params={"model": "anthropic/claude-haiku-3-5"},
        headers={"Authorization": f"Bearer {admin_setup.admin_key_a}"},
    )
    body = r.json()
    assert body["totals"]["requests"] == 1
    assert body["items"][0]["model"] == "anthropic/claude-haiku-3-5"


@pytest.mark.asyncio
async def test_filter_by_status(admin_setup: AdminSetup) -> None:
    r = await admin_setup.client.get(
        "/v1/admin/usage",
        params={"status": "error"},
        headers={"Authorization": f"Bearer {admin_setup.admin_key_a}"},
    )
    body = r.json()
    assert body["totals"]["requests"] == 1
    assert body["items"][0]["status"] == "error"


@pytest.mark.asyncio
async def test_filter_by_time_range(admin_setup: AdminSetup) -> None:
    """Filter to the last 2 hours — should pick up the 1h-ago groq row, the
    30min-ago error row, and the 5min-ago haiku success row (3 total). The
    2-days-ago and 1-day-ago rows must be excluded."""
    start = (datetime.now(tz=UTC) - timedelta(hours=2)).isoformat()
    r = await admin_setup.client.get(
        "/v1/admin/usage",
        params={"start_ts": start},
        headers={"Authorization": f"Bearer {admin_setup.admin_key_a}"},
    )
    body = r.json()
    assert body["totals"]["requests"] == 3


# --------------------------------------------------------------------------- #
# Tenant isolation                                                            #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_tenant_b_admin_cannot_see_tenant_a_rows(admin_setup: AdminSetup) -> None:
    """The most important security invariant: admin key for tenant B sees
    only B's rows (1), never A's (5). A bug in the WHERE clause would
    leak cross-tenant data."""
    r = await admin_setup.client.get(
        "/v1/admin/usage",
        headers={"Authorization": f"Bearer {admin_setup.admin_key_b}"},
    )
    body = r.json()
    assert body["totals"]["requests"] == 1
    assert body["items"][0]["tenant_id"] == admin_setup.tenant_b


@pytest.mark.asyncio
async def test_explicit_cross_tenant_filter_is_silently_dropped(
    admin_setup: AdminSetup,
) -> None:
    """Even if tenant A's admin tries to pass tenant B's team_id as a filter,
    the tenant_id scope is enforced first — they get zero rows, not B's data."""
    r = await admin_setup.client.get(
        "/v1/admin/usage",
        # Caller is admin_a; team passed doesn't belong to tenant A
        params={"team_id": "any-team-id-from-tenant-b"},
        headers={"Authorization": f"Bearer {admin_setup.admin_key_a}"},
    )
    body = r.json()
    assert body["totals"]["requests"] == 0


# --------------------------------------------------------------------------- #
# Pagination                                                                  #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_limit_truncates_items_but_totals_cover_full_set(
    admin_setup: AdminSetup,
) -> None:
    """Page size 2 returns 2 items but totals still reflect all 5 — this is
    the whole reason the endpoint computes totals separately from items."""
    r = await admin_setup.client.get(
        "/v1/admin/usage",
        params={"limit": 2},
        headers={"Authorization": f"Bearer {admin_setup.admin_key_a}"},
    )
    body = r.json()
    assert len(body["items"]) == 2
    assert body["totals"]["requests"] == 5
    assert body["limit"] == 2
    assert body["offset"] == 0


@pytest.mark.asyncio
async def test_offset_advances_through_pages(admin_setup: AdminSetup) -> None:
    headers = {"Authorization": f"Bearer {admin_setup.admin_key_a}"}
    page1 = (await admin_setup.client.get("/v1/admin/usage", params={"limit": 2, "offset": 0}, headers=headers)).json()
    page2 = (await admin_setup.client.get("/v1/admin/usage", params={"limit": 2, "offset": 2}, headers=headers)).json()
    page3 = (await admin_setup.client.get("/v1/admin/usage", params={"limit": 2, "offset": 4}, headers=headers)).json()

    ids_seen = {item["id"] for item in page1["items"]} | {
        item["id"] for item in page2["items"]
    } | {item["id"] for item in page3["items"]}
    assert len(ids_seen) == 5  # all rows, no duplicates across pages
    assert len(page3["items"]) == 1  # last page has the leftover row


@pytest.mark.asyncio
async def test_limit_above_max_is_rejected(admin_setup: AdminSetup) -> None:
    """limit > 1000 → 422 (Pydantic validation), not silently clamped."""
    r = await admin_setup.client.get(
        "/v1/admin/usage",
        params={"limit": 5000},
        headers={"Authorization": f"Bearer {admin_setup.admin_key_a}"},
    )
    assert r.status_code == 422


# --------------------------------------------------------------------------- #
# Phase 5.5: bidirectional scope enforcement                                  #
# --------------------------------------------------------------------------- #
#
# Phase 5.3 already proved a ``chat:write`` key gets 403 on ``/admin/usage``.
# Phase 5.5 adds the inverse and multi-scope cases:
#
#   - An ``admin:usage``-only key gets 403 on ``/chat/completions`` —
#     least-privilege MUST go both directions, or a leaked admin key
#     could be turned into a free LLM endpoint.
#   - A key with BOTH scopes passes both gates — the comma-separated
#     scope list works as a set, not a single-string match.


@pytest.mark.asyncio
async def test_admin_only_key_cannot_hit_chat_endpoint(admin_setup: AdminSetup) -> None:
    """The least-privilege guard in the OTHER direction: an admin:usage key
    must be rejected at /chat/completions. A leaked admin key shouldn't
    become a free chat endpoint."""
    r = await admin_setup.client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {admin_setup.admin_key_a}"},
        json={
            "model": "anthropic/claude-opus-4-7",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert r.status_code == 403
    assert "chat:write" in r.text


@pytest.mark.asyncio
async def test_multi_scope_key_passes_both_gates(
    admin_setup: AdminSetup, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A key issued with 'chat:write admin:usage' must satisfy BOTH gates.
    Scopes are space-separated set membership, not single-string equality.

    Inserts a fresh multi-scope key into the existing test DB rather than
    rebuilding the fixture — keeps this focused on the scope semantics."""
    import httpx
    import respx

    from pronaos.auth.api_keys import generate_api_key, hash_key
    from pronaos.db.models import ApiKey
    from pronaos.providers.anthropic import ANTHROPIC_API_URL

    # Add a multi-scope key onto tenant A's existing team.
    sm = admin_setup.client._transport.app.state.db_sessionmaker  # type: ignore[attr-defined]
    full, prefix = generate_api_key("test")
    async with sm() as session:
        multi = ApiKey(
            team_id=admin_setup.team_a1,
            prefix=prefix,
            key_hash=hash_key(full),
            scopes="chat:write admin:usage",
            label="multi",
        )
        session.add(multi)
        await session.commit()

    headers = {"Authorization": f"Bearer {full}"}

    # Gate 1: admin endpoint accepts the multi-scope key
    r1 = await admin_setup.client.get("/v1/admin/usage", headers=headers)
    assert r1.status_code == 200, r1.text

    # Gate 2: chat endpoint accepts the same key. Mock Anthropic so we
    # don't make a real call.
    with respx.mock:
        respx.post(ANTHROPIC_API_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "msg_01",
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "ok"}],
                    "model": "claude-opus-4-7",
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            )
        )
        r2 = await admin_setup.client.post(
            "/v1/chat/completions",
            headers=headers,
            json={
                "model": "anthropic/claude-opus-4-7",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
    assert r2.status_code == 200, r2.text
