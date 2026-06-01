"""Tests for the Phase 47 PromptCacheObserver.

Uses fakeredis to give the observer a real Redis backend without
needing a live one. Covers:

- record() + snapshot() round-trip
- hit_rate property computed correctly
- empty snapshot when no observations
- fail-open semantics (None redis)
- reset() wipes the team's state
- multiple fqmns per team
- multiple teams namespaced separately
"""

from __future__ import annotations

import pytest
from fakeredis.aioredis import FakeRedis

from pronaos.core.prompt_cache_observer import (
    DEFAULT_TTL_SECONDS,
    PromptCacheObserver,
    PromptCacheStat,
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
async def observer(redis: FakeRedis) -> PromptCacheObserver:  # type: ignore[type-arg]
    return PromptCacheObserver(redis)


class TestPromptCacheStat:
    def test_hit_rate_with_cache_hits(self) -> None:
        stat = PromptCacheStat(
            fqmn="anthropic/claude-sonnet-4-5",
            n_samples=5,
            prompt_tokens=200,
            cached_tokens=800,
            saved_hcents=720,
        )
        # 800 cached out of 1000 total input = 0.8 hit rate
        assert stat.hit_rate == pytest.approx(0.8)

    def test_hit_rate_zero_when_no_input(self) -> None:
        stat = PromptCacheStat(
            fqmn="groq/llama-3.1-8b",
            n_samples=1,
            prompt_tokens=0,
            cached_tokens=0,
            saved_hcents=0,
        )
        assert stat.hit_rate == 0.0

    def test_hit_rate_zero_when_no_cache(self) -> None:
        stat = PromptCacheStat(
            fqmn="groq/llama-3.1-8b",
            n_samples=10,
            prompt_tokens=5000,
            cached_tokens=0,
            saved_hcents=0,
        )
        assert stat.hit_rate == 0.0


class TestPromptCacheObserverRecordAndSnapshot:
    @pytest.mark.asyncio
    async def test_single_record_visible_in_snapshot(
        self, observer: PromptCacheObserver
    ) -> None:
        await observer.record(
            team_id="t1",
            fqmn="anthropic/claude-sonnet-4-5",
            prompt_tokens=100,
            cached_tokens=400,
            saved_hcents=360,
        )
        snap = await observer.snapshot("t1")
        assert set(snap.keys()) == {"anthropic/claude-sonnet-4-5"}
        stat = snap["anthropic/claude-sonnet-4-5"]
        assert stat.n_samples == 1
        assert stat.prompt_tokens == 100
        assert stat.cached_tokens == 400
        assert stat.saved_hcents == 360
        assert stat.hit_rate == pytest.approx(400 / 500)

    @pytest.mark.asyncio
    async def test_multiple_records_aggregate(
        self, observer: PromptCacheObserver
    ) -> None:
        for _ in range(5):
            await observer.record(
                team_id="t1",
                fqmn="anthropic/claude-sonnet-4-5",
                prompt_tokens=100,
                cached_tokens=400,
                saved_hcents=360,
            )
        snap = await observer.snapshot("t1")
        stat = snap["anthropic/claude-sonnet-4-5"]
        assert stat.n_samples == 5
        assert stat.prompt_tokens == 500
        assert stat.cached_tokens == 2000
        assert stat.saved_hcents == 1800

    @pytest.mark.asyncio
    async def test_multiple_fqmns_per_team(
        self, observer: PromptCacheObserver
    ) -> None:
        await observer.record(
            team_id="t1",
            fqmn="anthropic/claude-sonnet-4-5",
            prompt_tokens=100,
            cached_tokens=400,
        )
        await observer.record(
            team_id="t1",
            fqmn="openai/gpt-4o",
            prompt_tokens=200,
            cached_tokens=200,
        )
        await observer.record(
            team_id="t1",
            fqmn="groq/llama-3.1-8b-instant",
            prompt_tokens=500,
            cached_tokens=0,
        )
        snap = await observer.snapshot("t1")
        assert set(snap.keys()) == {
            "anthropic/claude-sonnet-4-5",
            "openai/gpt-4o",
            "groq/llama-3.1-8b-instant",
        }
        assert snap["anthropic/claude-sonnet-4-5"].hit_rate == pytest.approx(0.8)
        assert snap["openai/gpt-4o"].hit_rate == pytest.approx(0.5)
        assert snap["groq/llama-3.1-8b-instant"].hit_rate == pytest.approx(0.0)

    @pytest.mark.asyncio
    async def test_teams_namespaced_independently(
        self, observer: PromptCacheObserver
    ) -> None:
        await observer.record(
            team_id="t1",
            fqmn="anthropic/claude-sonnet-4-5",
            prompt_tokens=100,
            cached_tokens=400,
        )
        await observer.record(
            team_id="t2",
            fqmn="openai/gpt-4o",
            prompt_tokens=200,
            cached_tokens=0,
        )
        snap_t1 = await observer.snapshot("t1")
        snap_t2 = await observer.snapshot("t2")
        assert set(snap_t1.keys()) == {"anthropic/claude-sonnet-4-5"}
        assert set(snap_t2.keys()) == {"openai/gpt-4o"}

    @pytest.mark.asyncio
    async def test_empty_snapshot_when_unknown_team(
        self, observer: PromptCacheObserver
    ) -> None:
        snap = await observer.snapshot("nonexistent")
        assert snap == {}

    @pytest.mark.asyncio
    async def test_record_with_fqmn_containing_slashes(
        self, observer: PromptCacheObserver
    ) -> None:
        """fqmns like 'groq/meta-llama/llama-4-scout' have multiple slashes —
        the field-name separator must use ':' not '/' so they roundtrip
        cleanly through Redis."""
        fqmn = "groq/meta-llama/llama-4-scout-17b-16e-instruct"
        await observer.record(
            team_id="t1",
            fqmn=fqmn,
            prompt_tokens=100,
            cached_tokens=50,
        )
        snap = await observer.snapshot("t1")
        assert set(snap.keys()) == {fqmn}
        assert snap[fqmn].n_samples == 1
        assert snap[fqmn].prompt_tokens == 100
        assert snap[fqmn].cached_tokens == 50


class TestPromptCacheObserverFailOpen:
    @pytest.mark.asyncio
    async def test_record_with_no_redis_is_noop(self) -> None:
        observer = PromptCacheObserver(None)
        # Should not raise and should not store anything.
        await observer.record(
            team_id="t1",
            fqmn="anthropic/claude-sonnet-4-5",
            prompt_tokens=100,
            cached_tokens=400,
        )
        snap = await observer.snapshot("t1")
        assert snap == {}

    @pytest.mark.asyncio
    async def test_snapshot_with_no_redis_returns_empty(self) -> None:
        observer = PromptCacheObserver(None)
        snap = await observer.snapshot("t1")
        assert snap == {}

    @pytest.mark.asyncio
    async def test_record_with_zero_tokens_is_noop(
        self, observer: PromptCacheObserver
    ) -> None:
        await observer.record(
            team_id="t1",
            fqmn="anthropic/claude-sonnet-4-5",
            prompt_tokens=0,
            cached_tokens=0,
        )
        snap = await observer.snapshot("t1")
        # Defensive: shouldn't add a sample for a no-op call.
        assert snap == {}


class TestPromptCacheObserverReset:
    @pytest.mark.asyncio
    async def test_reset_wipes_team_state(
        self, observer: PromptCacheObserver
    ) -> None:
        await observer.record(
            team_id="t1",
            fqmn="anthropic/claude-sonnet-4-5",
            prompt_tokens=100,
            cached_tokens=400,
        )
        assert (await observer.snapshot("t1")) != {}
        await observer.reset("t1")
        assert (await observer.snapshot("t1")) == {}

    @pytest.mark.asyncio
    async def test_reset_does_not_affect_other_teams(
        self, observer: PromptCacheObserver
    ) -> None:
        await observer.record(
            team_id="t1",
            fqmn="anthropic/claude-sonnet-4-5",
            prompt_tokens=100,
            cached_tokens=400,
        )
        await observer.record(
            team_id="t2",
            fqmn="openai/gpt-4o",
            prompt_tokens=200,
            cached_tokens=100,
        )
        await observer.reset("t1")
        assert (await observer.snapshot("t1")) == {}
        snap_t2 = await observer.snapshot("t2")
        assert set(snap_t2.keys()) == {"openai/gpt-4o"}

    @pytest.mark.asyncio
    async def test_reset_with_no_redis_is_noop(self) -> None:
        observer = PromptCacheObserver(None)
        # Should not raise.
        await observer.reset("t1")


class TestPromptCacheObserverTTL:
    @pytest.mark.asyncio
    async def test_default_ttl_is_14_days(self) -> None:
        assert DEFAULT_TTL_SECONDS == 14 * 24 * 3600
