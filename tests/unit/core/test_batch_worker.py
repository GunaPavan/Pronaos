"""Tests for the Phase 59 BatchWorker.

The worker reconciles in-flight batches against the provider's
poll endpoint, finalises completed ones by parsing their result
JSONL, and writes per-sub-request usage rows at the half-priced
rate.

We mock the BatchClient at the protocol boundary — these tests
are not about HTTP wire shape (covered by ``test_batches.py``),
they're about the per-row state machine + DB writes.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from pronaos.config import Settings
from pronaos.core.batch_worker import BatchWorker
from pronaos.core.batches import BatchStatus, BatchSubmission
from pronaos.db.models import Base, Batch, UsageRecord

# --------------------------------------------------------------------------- #
# Mock BatchClient                                                            #
# --------------------------------------------------------------------------- #


class _MockClient:
    """In-memory BatchClient that scripts poll() + retrieve_results()
    responses per call. Each test wires the script it needs."""

    def __init__(self) -> None:
        self.poll_responses: list[BatchStatus] = []
        self.retrieve_jsonl: str = ""
        self.cancel_calls: list[str] = []
        self.closed: bool = False

    async def submit(self, *, requests_jsonl: str) -> BatchSubmission:
        return BatchSubmission(
            provider_batch_id="prov_001", initial_status="validating"
        )

    async def poll(self, *, provider_batch_id: str) -> BatchStatus:
        if not self.poll_responses:
            raise AssertionError("test ran out of scripted poll responses")
        return self.poll_responses.pop(0)

    async def retrieve_results(self, *, results_handle: str) -> str:
        return self.retrieve_jsonl

    async def cancel(self, *, provider_batch_id: str) -> None:
        self.cancel_calls.append(provider_batch_id)

    async def aclose(self) -> None:
        self.closed = True


# --------------------------------------------------------------------------- #
# DB fixture                                                                  #
# --------------------------------------------------------------------------- #


@pytest_asyncio.fixture
async def sm() -> Any:
    """Fresh in-memory SQLite per test."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    smkr = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield smkr
    finally:
        await engine.dispose()


def _make_settings() -> Settings:
    # OpenAI key empty by default — the worker will skip rows whose
    # provider has no credentials. Tests that need credentials inject
    # them via attribute write.
    return Settings(
        secret_key="x" * 64,
        openai_api_key="sk-test",
        anthropic_api_key="sk-ant-test",
    )


def _row(**overrides: Any) -> Batch:
    base = {
        "id": "pron_batch_test_001",
        "tenant_id": "t1",
        "team_id": "team1",
        "key_id": "k1",
        "provider": "openai",
        "provider_batch_id": "prov_001",
        "status": "in_progress",
        "endpoint": "/v1/chat/completions",
        "completion_window": "24h",
        "request_count": 3,
        "completed_count": 0,
        "failed_count": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cost_hcents": 0,
        "created_at": datetime.now(UTC),
        "input_payload": "",
        "output_payload": "",
    }
    base.update(overrides)
    return Batch(**base)


# --------------------------------------------------------------------------- #
# Tests                                                                       #
# --------------------------------------------------------------------------- #


class TestTickStatusSync:
    @pytest.mark.asyncio
    async def test_tick_updates_in_flight_counts(self, sm: Any) -> None:
        async with sm() as session:
            session.add(_row(status="in_progress"))
            await session.commit()

        worker = BatchWorker(sessionmaker=sm, settings=_make_settings())
        client = _MockClient()
        client.poll_responses.append(
            BatchStatus(
                provider_batch_id="prov_001",
                status="in_progress",
                request_count=3,
                completed_count=2,
                failed_count=0,
            )
        )
        # Inject our mock client into the worker's per-tick cache.
        worker._client_for = lambda provider, cache: client  # type: ignore[method-assign]

        n = await worker.tick()
        assert n == 1

        async with sm() as session:
            row = await session.get(Batch, "pron_batch_test_001")
            assert row is not None
            assert row.status == "in_progress"
            assert row.completed_count == 2

    @pytest.mark.asyncio
    async def test_tick_skips_terminal_rows(self, sm: Any) -> None:
        async with sm() as session:
            session.add(_row(id="b_done", status="completed"))
            session.add(_row(id="b_fail", status="failed"))
            session.add(_row(id="b_canc", status="cancelled"))
            await session.commit()

        worker = BatchWorker(sessionmaker=sm, settings=_make_settings())
        # The mock client has NO scripted responses; if any terminal
        # row gets polled the mock will raise.
        worker._client_for = lambda provider, cache: _MockClient()  # type: ignore[method-assign]

        n = await worker.tick()
        assert n == 0

    @pytest.mark.asyncio
    async def test_tick_marks_failed_on_missing_provider_id(
        self, sm: Any
    ) -> None:
        """A row with provider_batch_id=None is pathological — we
        fail it rather than crash."""
        async with sm() as session:
            session.add(_row(provider_batch_id=None))
            await session.commit()

        worker = BatchWorker(sessionmaker=sm, settings=_make_settings())
        worker._client_for = lambda provider, cache: _MockClient()  # type: ignore[method-assign]

        await worker.tick()

        async with sm() as session:
            row = await session.get(Batch, "pron_batch_test_001")
            assert row is not None
            assert row.status == "failed"
            assert row.error_message == "missing provider_batch_id"
            assert row.completed_at is not None

    @pytest.mark.asyncio
    async def test_tick_skips_when_no_credentials(self, sm: Any) -> None:
        async with sm() as session:
            session.add(_row())
            await session.commit()
        # Settings with no API keys — _client_for returns None and
        # the row should be left alone.
        s = Settings(secret_key="x" * 64, openai_api_key=None, anthropic_api_key=None)
        worker = BatchWorker(sessionmaker=sm, settings=s)

        await worker.tick()

        async with sm() as session:
            row = await session.get(Batch, "pron_batch_test_001")
            assert row is not None
            assert row.status == "in_progress"  # unchanged
            assert row.completed_count == 0


class TestTickFinalize:
    @pytest.mark.asyncio
    async def test_completed_writes_usage_rows_at_half_price(
        self, sm: Any
    ) -> None:
        """When a batch transitions to ``completed``, the worker
        fetches result JSONL, parses it, and writes one
        UsageRecord per successful sub-request at the batch rate."""
        async with sm() as session:
            session.add(_row(status="in_progress", request_count=2))
            await session.commit()

        worker = BatchWorker(sessionmaker=sm, settings=_make_settings())
        client = _MockClient()
        client.poll_responses.append(
            BatchStatus(
                provider_batch_id="prov_001",
                status="completed",
                request_count=2,
                completed_count=2,
                failed_count=0,
                results_handle="file-out-001",
            )
        )
        client.retrieve_jsonl = (
            json.dumps(
                {
                    "custom_id": "req-a",
                    "response": {
                        "body": {
                            "model": "gpt-4o-mini",
                            "usage": {
                                "prompt_tokens": 100,
                                "completion_tokens": 50,
                            },
                        }
                    },
                    "error": None,
                }
            )
            + "\n"
            + json.dumps(
                {
                    "custom_id": "req-b",
                    "response": {
                        "body": {
                            "model": "gpt-4o-mini",
                            "usage": {
                                "prompt_tokens": 50,
                                "completion_tokens": 20,
                            },
                        }
                    },
                    "error": None,
                }
            )
            + "\n"
        )
        worker._client_for = lambda provider, cache: client  # type: ignore[method-assign]

        await worker.tick()

        async with sm() as session:
            row = await session.get(Batch, "pron_batch_test_001")
            assert row is not None
            assert row.status == "completed"
            assert row.completed_count == 2
            assert row.failed_count == 0
            assert row.prompt_tokens == 150
            assert row.completion_tokens == 70
            assert row.completed_at is not None
            # Result blob persisted for replay.
            assert "req-a" in row.output_payload

            # Per-sub-request usage rows landed with the team's ids
            # and the batch_id#custom_id request_id.
            from sqlalchemy import select

            usage = (
                (
                    await session.execute(
                        select(UsageRecord).where(UsageRecord.team_id == "team1")
                    )
                )
                .scalars()
                .all()
            )
            assert len(usage) == 2
            for u in usage:
                assert u.status == "batch_success"
                assert u.request_id is not None
                assert u.request_id.startswith("pron_batch_test_001#")

    @pytest.mark.asyncio
    async def test_failed_terminal_does_not_write_usage(
        self, sm: Any
    ) -> None:
        async with sm() as session:
            session.add(_row(status="in_progress"))
            await session.commit()

        worker = BatchWorker(sessionmaker=sm, settings=_make_settings())
        client = _MockClient()
        client.poll_responses.append(
            BatchStatus(
                provider_batch_id="prov_001",
                status="failed",
                request_count=3,
                completed_count=0,
                failed_count=3,
                error_message="provider rejected all",
            )
        )
        worker._client_for = lambda provider, cache: client  # type: ignore[method-assign]

        await worker.tick()

        async with sm() as session:
            row = await session.get(Batch, "pron_batch_test_001")
            assert row is not None
            assert row.status == "failed"
            assert row.error_message == "provider rejected all"
            assert row.completed_at is not None
            # No usage rows because no sub-requests succeeded.
            from sqlalchemy import select

            usage = (
                (await session.execute(select(UsageRecord))).scalars().all()
            )
            assert usage == []


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_start_then_stop_is_idempotent(self, sm: Any) -> None:
        worker = BatchWorker(
            sessionmaker=sm, settings=_make_settings(), poll_interval_seconds=1
        )
        worker.start()
        worker.start()  # second start is a no-op
        await worker.stop()
        await worker.stop()  # second stop is a no-op
        # The internal task should be cleared.
        assert worker._task is None
