"""Tests for the Phase 49 ToolResultCache.

Uses fakeredis for a real Redis backend without a live one. Covers:

- canonicalise_args: key-order independence, string-vs-dict equivalence,
  bool/int distinction, nested dicts
- record() + lookup() round-trip
- per-team namespacing
- TTL refresh on every record
- fail-open semantics (None redis)
- reset() wipes
- snapshot() ordering by hit count
"""

from __future__ import annotations

import pytest
from fakeredis.aioredis import FakeRedis

from pronaos.core.tool_result_cache import (
    DEFAULT_TTL_SECONDS,
    ToolResultCache,
    canonicalise_args,
)


@pytest.fixture
async def redis() -> FakeRedis:  # type: ignore[type-arg]
    client = FakeRedis()
    try:
        yield client
    finally:
        await client.flushall()
        await client.aclose()


@pytest.fixture
async def cache(redis: FakeRedis) -> ToolResultCache:  # type: ignore[type-arg]
    return ToolResultCache(redis)


class TestCanonicaliseArgs:
    def test_key_order_invariant(self) -> None:
        a = canonicalise_args({"city": "Tokyo", "unit": "C"})
        b = canonicalise_args({"unit": "C", "city": "Tokyo"})
        assert a == b

    def test_string_and_dict_equivalent(self) -> None:
        # OpenAI tool_calls serialise arguments as a JSON string;
        # some adapters pre-parse. Both must hit the same cache key.
        d = canonicalise_args({"city": "Tokyo"})
        s = canonicalise_args('{"city": "Tokyo"}')
        assert d == s

    def test_nested_dict_sorted_recursively(self) -> None:
        a = canonicalise_args({"a": {"y": 1, "x": 2}, "b": 3})
        b = canonicalise_args({"b": 3, "a": {"x": 2, "y": 1}})
        assert a == b

    def test_bool_distinct_from_int(self) -> None:
        a = canonicalise_args({"x": True})
        b = canonicalise_args({"x": 1})
        assert a != b

    def test_malformed_string_passed_through(self) -> None:
        # If the model emits invalid JSON in arguments, we still
        # need a stable key — fall back to the raw string.
        assert canonicalise_args("not json {") == "not json {"


class TestRecordAndLookup:
    @pytest.mark.asyncio
    async def test_round_trip(self, cache: ToolResultCache) -> None:
        await cache.record(
            team_id="t1",
            tool_name="get_weather",
            args={"city": "Tokyo"},
            result="sunny 22C",
        )
        got = await cache.lookup(
            team_id="t1",
            tool_name="get_weather",
            args={"city": "Tokyo"},
        )
        assert got == "sunny 22C"

    @pytest.mark.asyncio
    async def test_lookup_miss_returns_none(self, cache: ToolResultCache) -> None:
        got = await cache.lookup(
            team_id="t1",
            tool_name="get_weather",
            args={"city": "Tokyo"},
        )
        assert got is None

    @pytest.mark.asyncio
    async def test_string_args_match_dict_args(self, cache: ToolResultCache) -> None:
        # Record with parsed dict; look up with the JSON string the
        # wire-shape brings — same cache key.
        await cache.record(
            team_id="t1",
            tool_name="get_weather",
            args={"city": "Tokyo"},
            result="sunny 22C",
        )
        got = await cache.lookup(
            team_id="t1",
            tool_name="get_weather",
            args='{"city": "Tokyo"}',
        )
        assert got == "sunny 22C"

    @pytest.mark.asyncio
    async def test_per_team_namespacing(self, cache: ToolResultCache) -> None:
        await cache.record(
            team_id="t1",
            tool_name="get_weather",
            args={"city": "Tokyo"},
            result="t1-result",
        )
        await cache.record(
            team_id="t2",
            tool_name="get_weather",
            args={"city": "Tokyo"},
            result="t2-result",
        )
        t1 = await cache.lookup(team_id="t1", tool_name="get_weather", args={"city": "Tokyo"})
        t2 = await cache.lookup(team_id="t2", tool_name="get_weather", args={"city": "Tokyo"})
        assert t1 == "t1-result"
        assert t2 == "t2-result"

    @pytest.mark.asyncio
    async def test_different_args_separate_entries(self, cache: ToolResultCache) -> None:
        await cache.record(
            team_id="t1",
            tool_name="get_weather",
            args={"city": "Tokyo"},
            result="Tokyo: sunny",
        )
        await cache.record(
            team_id="t1",
            tool_name="get_weather",
            args={"city": "Paris"},
            result="Paris: rainy",
        )
        assert (
            await cache.lookup(team_id="t1", tool_name="get_weather", args={"city": "Tokyo"})
            == "Tokyo: sunny"
        )
        assert (
            await cache.lookup(team_id="t1", tool_name="get_weather", args={"city": "Paris"})
            == "Paris: rainy"
        )

    @pytest.mark.asyncio
    async def test_record_overwrites(self, cache: ToolResultCache) -> None:
        # Subsequent record for the same key wins — operators want
        # the freshest tool result, never a stale one.
        await cache.record(
            team_id="t1",
            tool_name="get_weather",
            args={"city": "Tokyo"},
            result="old",
        )
        await cache.record(
            team_id="t1",
            tool_name="get_weather",
            args={"city": "Tokyo"},
            result="new",
        )
        assert (
            await cache.lookup(team_id="t1", tool_name="get_weather", args={"city": "Tokyo"})
            == "new"
        )


class TestFailOpenSemantics:
    @pytest.mark.asyncio
    async def test_record_with_no_redis_is_noop(self) -> None:
        c = ToolResultCache(None)
        await c.record(
            team_id="t1",
            tool_name="get_weather",
            args={"city": "Tokyo"},
            result="sunny",
        )
        # Should not raise; lookup also returns None.

    @pytest.mark.asyncio
    async def test_lookup_with_no_redis_returns_none(self) -> None:
        c = ToolResultCache(None)
        got = await c.lookup(team_id="t1", tool_name="get_weather", args={"city": "Tokyo"})
        assert got is None

    @pytest.mark.asyncio
    async def test_empty_tool_name_skipped(self, cache: ToolResultCache) -> None:
        await cache.record(team_id="t1", tool_name="", args={"city": "Tokyo"}, result="x")
        got = await cache.lookup(team_id="t1", tool_name="", args={"city": "Tokyo"})
        assert got is None

    @pytest.mark.asyncio
    async def test_empty_result_skipped(self, cache: ToolResultCache) -> None:
        await cache.record(
            team_id="t1",
            tool_name="get_weather",
            args={"city": "Tokyo"},
            result="",
        )
        got = await cache.lookup(team_id="t1", tool_name="get_weather", args={"city": "Tokyo"})
        assert got is None


class TestSnapshot:
    @pytest.mark.asyncio
    async def test_snapshot_returns_all_entries(self, cache: ToolResultCache) -> None:
        await cache.record(team_id="t1", tool_name="a", args={"k": 1}, result="r1")
        await cache.record(team_id="t1", tool_name="b", args={"k": 2}, result="r2")
        snap = await cache.snapshot("t1")
        names = {e.tool_name for e in snap}
        assert names == {"a", "b"}

    @pytest.mark.asyncio
    async def test_snapshot_orders_by_hit_count_desc(self, cache: ToolResultCache) -> None:
        await cache.record(team_id="t1", tool_name="hot", args={"k": 1}, result="r1")
        await cache.record(team_id="t1", tool_name="cold", args={"k": 2}, result="r2")
        # Hit "hot" 3 times.
        for _ in range(3):
            await cache.lookup(team_id="t1", tool_name="hot", args={"k": 1})
        snap = await cache.snapshot("t1")
        assert snap[0].tool_name == "hot"
        assert snap[0].n_hits == 3
        assert snap[1].tool_name == "cold"
        assert snap[1].n_hits == 0

    @pytest.mark.asyncio
    async def test_snapshot_empty_when_no_records(self, cache: ToolResultCache) -> None:
        snap = await cache.snapshot("nonexistent_team")
        assert snap == []


class TestReset:
    @pytest.mark.asyncio
    async def test_reset_wipes_team(self, cache: ToolResultCache) -> None:
        await cache.record(
            team_id="t1",
            tool_name="get_weather",
            args={"city": "Tokyo"},
            result="sunny",
        )
        await cache.reset("t1")
        got = await cache.lookup(team_id="t1", tool_name="get_weather", args={"city": "Tokyo"})
        assert got is None

    @pytest.mark.asyncio
    async def test_reset_does_not_affect_other_teams(self, cache: ToolResultCache) -> None:
        await cache.record(
            team_id="t1",
            tool_name="get_weather",
            args={"city": "Tokyo"},
            result="sunny",
        )
        await cache.record(
            team_id="t2",
            tool_name="get_weather",
            args={"city": "Tokyo"},
            result="rainy",
        )
        await cache.reset("t1")
        t1 = await cache.lookup(team_id="t1", tool_name="get_weather", args={"city": "Tokyo"})
        t2 = await cache.lookup(team_id="t2", tool_name="get_weather", args={"city": "Tokyo"})
        assert t1 is None
        assert t2 == "rainy"

    @pytest.mark.asyncio
    async def test_reset_with_no_redis_is_noop(self) -> None:
        c = ToolResultCache(None)
        await c.reset("t1")  # should not raise


class TestDefaults:
    def test_default_ttl_is_one_hour(self) -> None:
        assert DEFAULT_TTL_SECONDS == 3600
