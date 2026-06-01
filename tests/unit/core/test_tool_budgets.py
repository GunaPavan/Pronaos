"""Unit tests for the Phase 37 per-tool budget logic.

Two surfaces under test:

1. ``pronaos.core.tool_budgets`` — pure helpers that decide whether a
   tool is over budget and strip the over-budget entries from a
   ``tools`` array. No DB, no I/O, just data transforms.
2. ``QuotaTracker.record_call`` — the SELECT-MODIFY-UPDATE that
   increments ``teams.tool_budgets[name].current_calls`` per emitted
   tool name. Runs against an in-memory SQLite DB so we exercise the
   real SQL path (JSON column write-back) without external infra.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from pronaos.core.quota import CompletedCall, QuotaTracker
from pronaos.core.tool_budgets import (
    is_over_budget,
    strip_over_budget_tools,
    tool_names_from_calls,
)
from pronaos.db.models import Base, Team, Tenant, UsageRecord

# --------------------------------------------------------------------------- #
# Pure helpers                                                                #
# --------------------------------------------------------------------------- #


class TestIsOverBudget:
    def test_none_budgets_never_over(self) -> None:
        assert is_over_budget(None, "web_search") is False

    def test_empty_dict_never_over(self) -> None:
        assert is_over_budget({}, "web_search") is False

    def test_absent_tool_never_over(self) -> None:
        budgets = {"web_search": {"limit_calls": 100, "current_calls": 100}}
        assert is_over_budget(budgets, "code_exec") is False

    def test_under_limit_not_over(self) -> None:
        budgets = {"web_search": {"limit_calls": 100, "current_calls": 50}}
        assert is_over_budget(budgets, "web_search") is False

    def test_at_limit_over(self) -> None:
        budgets = {"web_search": {"limit_calls": 100, "current_calls": 100}}
        assert is_over_budget(budgets, "web_search") is True

    def test_above_limit_over(self) -> None:
        budgets = {"web_search": {"limit_calls": 100, "current_calls": 101}}
        assert is_over_budget(budgets, "web_search") is True

    def test_zero_limit_treated_as_no_cap(self) -> None:
        # A limit of 0 is the "uncapped but tracked" marker; reach for
        # --remove to actually drop the entry. Matches the CLI semantic.
        budgets = {"web_search": {"limit_calls": 0, "current_calls": 0}}
        assert is_over_budget(budgets, "web_search") is False

    def test_malformed_entry_treated_as_no_cap(self) -> None:
        # A string where an int should be — operator wrote garbage.
        # Defensive: prefer letting the request through than crashing.
        budgets = {"web_search": {"limit_calls": "100", "current_calls": 5}}
        assert is_over_budget(budgets, "web_search") is False  # type: ignore[arg-type]

    def test_missing_current_treated_as_zero(self) -> None:
        budgets = {"web_search": {"limit_calls": 100}}
        assert is_over_budget(budgets, "web_search") is False  # type: ignore[arg-type]


class TestStripOverBudgetTools:
    def test_no_tools_returns_unchanged(self) -> None:
        new, stripped = strip_over_budget_tools(None, {"web_search": {"limit_calls": 1, "current_calls": 1}})
        assert new is None
        assert stripped == []

    def test_no_budgets_returns_unchanged(self) -> None:
        tools = [{"type": "function", "function": {"name": "web_search"}}]
        new, stripped = strip_over_budget_tools(tools, None)
        assert new == tools
        assert stripped == []

    def test_strips_over_budget_entry(self) -> None:
        tools = [
            {"type": "function", "function": {"name": "web_search"}},
            {"type": "function", "function": {"name": "code_exec"}},
        ]
        budgets = {"web_search": {"limit_calls": 5, "current_calls": 5}}
        new, stripped = strip_over_budget_tools(tools, budgets)
        assert new == [{"type": "function", "function": {"name": "code_exec"}}]
        assert stripped == ["web_search"]

    def test_strips_multiple(self) -> None:
        tools = [
            {"type": "function", "function": {"name": "a"}},
            {"type": "function", "function": {"name": "b"}},
            {"type": "function", "function": {"name": "c"}},
        ]
        budgets = {
            "a": {"limit_calls": 1, "current_calls": 1},
            "c": {"limit_calls": 2, "current_calls": 3},
        }
        new, stripped = strip_over_budget_tools(tools, budgets)
        assert [t["function"]["name"] for t in (new or [])] == ["b"]
        assert sorted(stripped) == ["a", "c"]

    def test_preserves_order(self) -> None:
        tools = [
            {"type": "function", "function": {"name": "z"}},
            {"type": "function", "function": {"name": "a"}},
        ]
        budgets: dict[str, dict[str, int]] = {}
        new, stripped = strip_over_budget_tools(tools, budgets)
        assert new == tools  # untouched
        assert stripped == []

    def test_keeps_unrecognised_entries(self) -> None:
        # Missing function name → pass through, can't correlate to budgets.
        tools = [
            {"type": "function", "function": {}},
            {"type": "function", "function": {"name": "web_search"}},
        ]
        budgets = {"web_search": {"limit_calls": 1, "current_calls": 1}}
        new, stripped = strip_over_budget_tools(tools, budgets)
        assert len(new or []) == 1
        assert (new or [])[0]["function"] == {}
        assert stripped == ["web_search"]


class TestToolNamesFromCalls:
    def test_none_returns_empty(self) -> None:
        assert tool_names_from_calls(None) == []

    def test_empty_returns_empty(self) -> None:
        assert tool_names_from_calls([]) == []

    def test_extracts_single(self) -> None:
        calls = [{"id": "1", "type": "function", "function": {"name": "web_search", "arguments": "{}"}}]
        assert tool_names_from_calls(calls) == ["web_search"]

    def test_extracts_multiple_preserves_order(self) -> None:
        calls = [
            {"id": "1", "type": "function", "function": {"name": "b"}},
            {"id": "2", "type": "function", "function": {"name": "a"}},
        ]
        assert tool_names_from_calls(calls) == ["b", "a"]

    def test_preserves_duplicates(self) -> None:
        # Same tool called twice in one response = two billable invocations.
        calls = [
            {"id": "1", "type": "function", "function": {"name": "web_search"}},
            {"id": "2", "type": "function", "function": {"name": "web_search"}},
        ]
        assert tool_names_from_calls(calls) == ["web_search", "web_search"]

    def test_skips_malformed_entries(self) -> None:
        calls = [
            {"id": "1", "type": "function", "function": {"name": "good"}},
            "not a dict",
            {"id": "2", "type": "function"},  # no function dict
            {"id": "3", "type": "function", "function": {"name": ""}},  # empty name
            {"id": "4", "type": "function", "function": {"name": 123}},  # non-string
        ]
        assert tool_names_from_calls(calls) == ["good"]  # type: ignore[list-item]


# --------------------------------------------------------------------------- #
# QuotaTracker.record_call with tool_budgets                                  #
# --------------------------------------------------------------------------- #


@pytest_asyncio.fixture
async def sessionmaker_() -> AsyncIterator[async_sessionmaker]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield sm
    finally:
        await engine.dispose()


async def _seed_team_with_tool_budgets(
    sm: async_sessionmaker,
    tool_budgets: dict[str, dict[str, int]] | None,
) -> tuple[str, str]:
    """Returns (tenant_id, team_id)."""
    async with sm() as session:
        tenant = Tenant(name="acme")
        session.add(tenant)
        await session.flush()
        team = Team(
            tenant_id=tenant.id,
            name="eng",
            monthly_token_budget=None,
            current_period_tokens=0,
            period_resets_at=datetime(2099, 1, 1, tzinfo=UTC),
            tool_budgets=tool_budgets,
        )
        session.add(team)
        await session.commit()
        return tenant.id, team.id


class TestRecordCallToolBudgets:
    @pytest.mark.asyncio
    async def test_no_tool_names_no_budget_update(
        self, sessionmaker_: async_sessionmaker
    ) -> None:
        """Plain chat call (no tool_calls) — tool_budgets stays untouched."""
        tenant_id, team_id = await _seed_team_with_tool_budgets(
            sessionmaker_, {"web_search": {"limit_calls": 100, "current_calls": 5}}
        )
        tracker = QuotaTracker()
        async with sessionmaker_() as session:
            await tracker.record_call(
                session,
                CompletedCall(
                    tenant_id=tenant_id,
                    team_id=team_id,
                    key_id="k1",
                    provider="openai",
                    model="gpt-4",
                    prompt_tokens=10,
                    completion_tokens=20,
                    cost_hcents=5,
                ),
            )
            await session.commit()
        async with sessionmaker_() as session:
            team = await session.get(Team, team_id)
            assert team is not None
            assert team.tool_budgets == {
                "web_search": {"limit_calls": 100, "current_calls": 5}
            }

    @pytest.mark.asyncio
    async def test_single_emitted_tool_increments(
        self, sessionmaker_: async_sessionmaker
    ) -> None:
        tenant_id, team_id = await _seed_team_with_tool_budgets(
            sessionmaker_, {"web_search": {"limit_calls": 100, "current_calls": 5}}
        )
        tracker = QuotaTracker()
        async with sessionmaker_() as session:
            await tracker.record_call(
                session,
                CompletedCall(
                    tenant_id=tenant_id,
                    team_id=team_id,
                    key_id="k1",
                    provider="openai",
                    model="gpt-4",
                    prompt_tokens=10,
                    completion_tokens=20,
                    cost_hcents=5,
                    tool_names=("web_search",),
                ),
            )
            await session.commit()
        async with sessionmaker_() as session:
            team = await session.get(Team, team_id)
            assert team is not None
            assert team.tool_budgets == {
                "web_search": {"limit_calls": 100, "current_calls": 6}
            }

    @pytest.mark.asyncio
    async def test_duplicate_tool_increments_twice(
        self, sessionmaker_: async_sessionmaker
    ) -> None:
        """Same tool emitted twice in one response = +2 on the counter."""
        tenant_id, team_id = await _seed_team_with_tool_budgets(
            sessionmaker_, {"web_search": {"limit_calls": 100, "current_calls": 0}}
        )
        tracker = QuotaTracker()
        async with sessionmaker_() as session:
            await tracker.record_call(
                session,
                CompletedCall(
                    tenant_id=tenant_id,
                    team_id=team_id,
                    key_id="k1",
                    provider="openai",
                    model="gpt-4",
                    prompt_tokens=10,
                    completion_tokens=20,
                    cost_hcents=5,
                    tool_names=("web_search", "web_search"),
                ),
            )
            await session.commit()
        async with sessionmaker_() as session:
            team = await session.get(Team, team_id)
            assert team is not None
            assert (team.tool_budgets or {})["web_search"]["current_calls"] == 2

    @pytest.mark.asyncio
    async def test_unconfigured_tool_silently_skipped(
        self, sessionmaker_: async_sessionmaker
    ) -> None:
        """Tool not in budgets dict = no auto-create, no counter."""
        tenant_id, team_id = await _seed_team_with_tool_budgets(
            sessionmaker_, {"web_search": {"limit_calls": 100, "current_calls": 5}}
        )
        tracker = QuotaTracker()
        async with sessionmaker_() as session:
            await tracker.record_call(
                session,
                CompletedCall(
                    tenant_id=tenant_id,
                    team_id=team_id,
                    key_id="k1",
                    provider="openai",
                    model="gpt-4",
                    prompt_tokens=10,
                    completion_tokens=20,
                    cost_hcents=5,
                    tool_names=("unknown_tool",),
                ),
            )
            await session.commit()
        async with sessionmaker_() as session:
            team = await session.get(Team, team_id)
            assert team is not None
            assert team.tool_budgets == {
                "web_search": {"limit_calls": 100, "current_calls": 5}
            }

    @pytest.mark.asyncio
    async def test_null_budgets_no_crash(
        self, sessionmaker_: async_sessionmaker
    ) -> None:
        """Team with no per-tool caps configured — record_call is a no-op
        on the budgets side but still writes the usage_record."""
        tenant_id, team_id = await _seed_team_with_tool_budgets(sessionmaker_, None)
        tracker = QuotaTracker()
        async with sessionmaker_() as session:
            await tracker.record_call(
                session,
                CompletedCall(
                    tenant_id=tenant_id,
                    team_id=team_id,
                    key_id="k1",
                    provider="openai",
                    model="gpt-4",
                    prompt_tokens=10,
                    completion_tokens=20,
                    cost_hcents=5,
                    tool_names=("web_search",),
                ),
            )
            await session.commit()
        async with sessionmaker_() as session:
            team = await session.get(Team, team_id)
            assert team is not None
            assert team.tool_budgets is None

    @pytest.mark.asyncio
    async def test_usage_record_tool_names_stored(
        self, sessionmaker_: async_sessionmaker
    ) -> None:
        """Comma-joined tool_names land on the usage_records row."""
        tenant_id, team_id = await _seed_team_with_tool_budgets(sessionmaker_, None)
        tracker = QuotaTracker()
        async with sessionmaker_() as session:
            await tracker.record_call(
                session,
                CompletedCall(
                    tenant_id=tenant_id,
                    team_id=team_id,
                    key_id="k1",
                    provider="openai",
                    model="gpt-4",
                    prompt_tokens=10,
                    completion_tokens=20,
                    cost_hcents=5,
                    tool_names=("web_search", "code_exec"),
                ),
            )
            await session.commit()
        async with sessionmaker_() as session:
            row = (
                await session.execute(
                    select(UsageRecord).where(UsageRecord.team_id == team_id)
                )
            ).scalar_one()
            assert row.tool_names == "web_search,code_exec"

    @pytest.mark.asyncio
    async def test_usage_record_no_tools_null(
        self, sessionmaker_: async_sessionmaker
    ) -> None:
        """No tool_calls = NULL in the column, not empty string."""
        tenant_id, team_id = await _seed_team_with_tool_budgets(sessionmaker_, None)
        tracker = QuotaTracker()
        async with sessionmaker_() as session:
            await tracker.record_call(
                session,
                CompletedCall(
                    tenant_id=tenant_id,
                    team_id=team_id,
                    key_id="k1",
                    provider="openai",
                    model="gpt-4",
                    prompt_tokens=10,
                    completion_tokens=20,
                    cost_hcents=5,
                ),
            )
            await session.commit()
        async with sessionmaker_() as session:
            row = (
                await session.execute(
                    select(UsageRecord).where(UsageRecord.team_id == team_id)
                )
            ).scalar_one()
            assert row.tool_names is None
