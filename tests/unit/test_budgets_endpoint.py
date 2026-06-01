"""HTTP-level tests for the Phase 64 budgets + timeseries surface.

Covers
------
- GET /v1/admin/budgets/{team_id} returns the team's caps + current
  period state; 404 on unknown team.
- PUT /v1/admin/budgets/{team_id} updates caps; null clears (unlimited);
  omitted field is unchanged.
- Scope enforcement: PUT requires admin:identity; GET requires admin:usage.
- GET /v1/admin/usage/timeseries returns dense buckets with correct sums.
- Timeseries rejects bad window (end <= start) and too-wide windows.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update

from pronaos.db.models import ApiKey, UsageRecord


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _grant_scope(sm, key_id: str, scopes: str) -> None:  # type: ignore[no-untyped-def]
    """Override the seeded key's scopes."""
    async with sm() as session:
        await session.execute(
            update(ApiKey).where(ApiKey.id == key_id).values(scopes=scopes)
        )
        await session.commit()


# --------------------------------------------------------------------------- #
# Budgets — GET / PUT                                                         #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_budget_get_returns_team_shape(auth_setup) -> None:  # type: ignore[no-untyped-def]
    await _grant_scope(auth_setup.sm, auth_setup.key_id, "admin:usage")
    r = await auth_setup.client.get(
        f"/v1/admin/budgets/{auth_setup.team_id}",
        headers=_auth(auth_setup.api_key),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["team_id"] == auth_setup.team_id
    # All 6 expected fields present.
    expected = {
        "team_id",
        "monthly_token_budget",
        "current_period_tokens",
        "monthly_cost_hcents_budget",
        "current_period_cost_hcents",
        "period_resets_at",
    }
    assert set(body.keys()) == expected
    # Default budgets are NULL (unlimited).
    assert body["monthly_token_budget"] is None
    assert body["monthly_cost_hcents_budget"] is None


@pytest.mark.asyncio
async def test_budget_get_404_for_unknown_team(auth_setup) -> None:  # type: ignore[no-untyped-def]
    await _grant_scope(auth_setup.sm, auth_setup.key_id, "admin:usage")
    r = await auth_setup.client.get(
        "/v1/admin/budgets/does_not_exist",
        headers=_auth(auth_setup.api_key),
    )
    assert r.status_code == 404
    assert r.json()["detail"]["type"] == "team_not_found"


@pytest.mark.asyncio
async def test_budget_put_sets_caps_and_persists(auth_setup) -> None:  # type: ignore[no-untyped-def]
    await _grant_scope(
        auth_setup.sm, auth_setup.key_id, "admin:usage admin:identity"
    )
    headers = _auth(auth_setup.api_key)

    r = await auth_setup.client.put(
        f"/v1/admin/budgets/{auth_setup.team_id}",
        json={
            "monthly_token_budget": 10_000_000,
            "monthly_cost_hcents_budget": 500_000,
        },
        headers=headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["monthly_token_budget"] == 10_000_000
    assert r.json()["monthly_cost_hcents_budget"] == 500_000

    # Reload via GET to confirm DB persistence.
    r = await auth_setup.client.get(
        f"/v1/admin/budgets/{auth_setup.team_id}", headers=headers
    )
    assert r.json()["monthly_token_budget"] == 10_000_000
    assert r.json()["monthly_cost_hcents_budget"] == 500_000


@pytest.mark.asyncio
async def test_budget_put_null_clears_cap(auth_setup) -> None:  # type: ignore[no-untyped-def]
    """Passing ``null`` explicitly should set the cap back to NULL
    (unlimited). Omitting the field entirely is a separate case."""
    await _grant_scope(
        auth_setup.sm, auth_setup.key_id, "admin:usage admin:identity"
    )
    headers = _auth(auth_setup.api_key)
    # First set a cap.
    await auth_setup.client.put(
        f"/v1/admin/budgets/{auth_setup.team_id}",
        json={"monthly_token_budget": 100},
        headers=headers,
    )
    # Then clear it.
    r = await auth_setup.client.put(
        f"/v1/admin/budgets/{auth_setup.team_id}",
        json={"monthly_token_budget": None},
        headers=headers,
    )
    assert r.status_code == 200
    assert r.json()["monthly_token_budget"] is None


@pytest.mark.asyncio
async def test_budget_put_only_touches_set_fields(auth_setup) -> None:  # type: ignore[no-untyped-def]
    """Partial update: setting only the cost cap must leave the token
    cap unchanged."""
    await _grant_scope(
        auth_setup.sm, auth_setup.key_id, "admin:usage admin:identity"
    )
    headers = _auth(auth_setup.api_key)
    # Seed both caps.
    await auth_setup.client.put(
        f"/v1/admin/budgets/{auth_setup.team_id}",
        json={
            "monthly_token_budget": 9_999,
            "monthly_cost_hcents_budget": 8_888,
        },
        headers=headers,
    )
    # Patch only the cost cap.
    r = await auth_setup.client.put(
        f"/v1/admin/budgets/{auth_setup.team_id}",
        json={"monthly_cost_hcents_budget": 7_777},
        headers=headers,
    )
    body = r.json()
    assert body["monthly_token_budget"] == 9_999, "token cap should not change"
    assert body["monthly_cost_hcents_budget"] == 7_777


@pytest.mark.asyncio
async def test_budget_put_negative_value_rejected(auth_setup) -> None:  # type: ignore[no-untyped-def]
    await _grant_scope(
        auth_setup.sm, auth_setup.key_id, "admin:usage admin:identity"
    )
    r = await auth_setup.client.put(
        f"/v1/admin/budgets/{auth_setup.team_id}",
        json={"monthly_token_budget": -1},
        headers=_auth(auth_setup.api_key),
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_budget_put_requires_admin_identity_scope(auth_setup) -> None:  # type: ignore[no-untyped-def]
    """A key with only admin:usage can READ but cannot WRITE."""
    await _grant_scope(auth_setup.sm, auth_setup.key_id, "admin:usage")
    r = await auth_setup.client.put(
        f"/v1/admin/budgets/{auth_setup.team_id}",
        json={"monthly_token_budget": 100},
        headers=_auth(auth_setup.api_key),
    )
    assert r.status_code == 403
    assert "admin:identity" in r.json()["detail"]


# --------------------------------------------------------------------------- #
# Usage timeseries                                                            #
# --------------------------------------------------------------------------- #


async def _seed_usage_rows(sm, tenant_id: str, team_id: str, key_id: str) -> None:  # type: ignore[no-untyped-def]
    """Plant 5 usage_records across 3 distinct days."""
    base = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
    async with sm() as session:
        for i, (offset_days, prompt, completion, cost) in enumerate(
            [
                (0, 100, 50, 10),  # day 1
                (0, 200, 100, 25),  # day 1
                (1, 50, 25, 5),  # day 2
                (2, 300, 150, 50),  # day 3
                (2, 400, 200, 75),  # day 3
            ]
        ):
            session.add(
                UsageRecord(
                    ts=base + timedelta(days=offset_days, hours=i),
                    tenant_id=tenant_id,
                    team_id=team_id,
                    key_id=key_id,
                    provider="openai",
                    model="gpt-4o-mini",
                    prompt_tokens=prompt,
                    completion_tokens=completion,
                    cost_hcents=cost,
                    status="success",
                )
            )
        await session.commit()


@pytest.mark.asyncio
async def test_timeseries_aggregates_by_day(auth_setup) -> None:  # type: ignore[no-untyped-def]
    await _grant_scope(auth_setup.sm, auth_setup.key_id, "admin:usage")
    await _seed_usage_rows(
        auth_setup.sm,
        auth_setup.tenant_id,
        auth_setup.team_id,
        auth_setup.key_id,
    )

    # Window: 5 days starting Jan 15.
    r = await auth_setup.client.get(
        "/v1/admin/usage/timeseries",
        params={
            "start_ts": "2026-01-15T00:00:00Z",
            "end_ts": "2026-01-20T00:00:00Z",
            "bucket": "day",
        },
        headers=_auth(auth_setup.api_key),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["bucket_size_seconds"] == 86_400
    # Dense buckets — 5 days requested.
    assert len(body["points"]) == 5

    # Verify sums by day-of-month → expected values.
    by_day = {
        datetime.fromtimestamp(p["bucket"], UTC).day: p for p in body["points"]
    }
    assert by_day[15]["requests"] == 2
    assert by_day[15]["prompt_tokens"] == 300  # 100 + 200
    assert by_day[15]["cost_hcents"] == 35  # 10 + 25
    assert by_day[16]["requests"] == 1
    assert by_day[16]["prompt_tokens"] == 50
    assert by_day[17]["requests"] == 2
    assert by_day[17]["cost_hcents"] == 125  # 50 + 75
    # Empty days still appear with zero counts.
    assert by_day[18]["requests"] == 0
    assert by_day[19]["requests"] == 0


@pytest.mark.asyncio
async def test_timeseries_rejects_inverted_window(auth_setup) -> None:  # type: ignore[no-untyped-def]
    await _grant_scope(auth_setup.sm, auth_setup.key_id, "admin:usage")
    r = await auth_setup.client.get(
        "/v1/admin/usage/timeseries",
        params={
            "start_ts": "2026-01-20T00:00:00Z",
            "end_ts": "2026-01-15T00:00:00Z",
        },
        headers=_auth(auth_setup.api_key),
    )
    assert r.status_code == 422
    assert r.json()["detail"]["type"] == "invalid_window"


@pytest.mark.asyncio
async def test_timeseries_rejects_too_wide_window(auth_setup) -> None:  # type: ignore[no-untyped-def]
    """Window > 1000 buckets at the requested granularity is rejected
    to prevent the dashboard accidentally fetching a 10-year hourly
    series."""
    await _grant_scope(auth_setup.sm, auth_setup.key_id, "admin:usage")
    r = await auth_setup.client.get(
        "/v1/admin/usage/timeseries",
        params={
            "start_ts": "2020-01-01T00:00:00Z",
            "end_ts": "2030-01-01T00:00:00Z",
            "bucket": "hour",
        },
        headers=_auth(auth_setup.api_key),
    )
    assert r.status_code == 422
    assert r.json()["detail"]["type"] == "window_too_wide"


@pytest.mark.asyncio
async def test_timeseries_filters_by_team(auth_setup) -> None:  # type: ignore[no-untyped-def]
    """Setting team_id filters to that team only."""
    await _grant_scope(auth_setup.sm, auth_setup.key_id, "admin:usage")
    await _seed_usage_rows(
        auth_setup.sm,
        auth_setup.tenant_id,
        auth_setup.team_id,
        auth_setup.key_id,
    )
    r = await auth_setup.client.get(
        "/v1/admin/usage/timeseries",
        params={
            "start_ts": "2026-01-15T00:00:00Z",
            "end_ts": "2026-01-20T00:00:00Z",
            "team_id": "other_team_id",
        },
        headers=_auth(auth_setup.api_key),
    )
    assert r.status_code == 200
    # All buckets zero — no rows for the foreign team.
    for p in r.json()["points"]:
        assert p["requests"] == 0
