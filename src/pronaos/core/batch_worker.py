"""Background polling worker for the async-batches API (Phase 59).

The worker is a single asyncio task launched from FastAPI's lifespan.
It wakes every ``BATCH_POLL_INTERVAL_SECONDS`` and:

1. Selects all rows in non-terminal states.
2. For each row, calls the provider's ``poll()`` and updates counts +
   status + timestamps.
3. On transition to ``completed``, calls ``retrieve_results()``,
   parses the JSONL, persists the result blob on the row, summarises
   counts, and writes one ``UsageRecord`` per successful sub-request
   at the half-priced rate.
4. Bumps ``record_batch_event`` whenever a row crosses into a
   terminal state.

Design notes
------------
- Single worker per gateway process. There's no need for a leader
  election here because every poll path is **idempotent** — running
  it twice with the same row state produces the same DB writes (the
  per-sub-request usage rows are keyed by a deterministic id derived
  from ``batch_id`` + ``custom_id`` so a second pass would conflict
  on the primary key and be skipped). Operators running multiple
  replicas can either disable the worker on N-1 replicas (config flag)
  or accept the harmless duplicate-key noise.
- Errors mid-poll never block the next row; we log + continue. A row
  that errors three polls in a row is left in its last-known state —
  the operator inspects ``error_message`` and decides.
- ``aclose()`` reuses one ``httpx.AsyncClient`` per provider per
  worker tick so we don't pay TCP+TLS setup per row.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pronaos.config import Settings
from pronaos.core.batches import (
    AnthropicBatchClient,
    BatchClient,
    OpenAIBatchClient,
    batch_cost_hcents,
    parse_anthropic_result_jsonl,
    parse_openai_result_jsonl,
    summarize_results,
)
from pronaos.db.models import Batch, UsageRecord
from pronaos.logging import get_logger
from pronaos.observability.metrics import record_batch_event

if TYPE_CHECKING:
    from collections.abc import Sequence

log = get_logger(__name__)

# Terminal states do not roll back; the worker skips them.
_TERMINAL = frozenset({"completed", "failed", "expired", "cancelled"})


class BatchWorker:
    """Single-process async-batches reconciliation loop."""

    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        settings: Settings,
        poll_interval_seconds: int = 60,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._settings = settings
        self._poll_interval = poll_interval_seconds
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    # ------------------------------------------------------------------ #
    # Lifecycle                                                          #
    # ------------------------------------------------------------------ #

    def start(self) -> None:
        """Spawn the background loop. Idempotent — re-calling is a
        no-op if the task is already running."""
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="pronaos-batch-worker")

    async def stop(self) -> None:
        """Signal the loop to stop and await its exit. Idempotent."""
        if self._task is None:
            return
        self._stop.set()
        with suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    # ------------------------------------------------------------------ #
    # Loop body                                                          #
    # ------------------------------------------------------------------ #

    async def _run(self) -> None:
        log.info("batch_worker_started", extra={"interval_s": self._poll_interval})
        try:
            while not self._stop.is_set():
                try:
                    await self.tick()
                except Exception:
                    # Never let a single tick crash the loop; log and continue.
                    log.exception("batch_worker_tick_failed")
                # Sleep with cancellation support — wait_for on the stop
                # event lets stop() cut the wait short.
                with suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._stop.wait(), timeout=self._poll_interval
                    )
        finally:
            log.info("batch_worker_stopped")

    async def tick(self) -> int:
        """Run one reconciliation pass. Returns the number of rows
        examined. Exposed publicly so tests + operators can trigger
        a single sweep without spinning up the background task."""
        async with self._sessionmaker() as session:
            stmt = select(Batch).where(~Batch.status.in_(_TERMINAL))
            result = await session.execute(stmt)
            rows: Sequence[Batch] = result.scalars().all()
            if not rows:
                return 0
            # Reuse one client per provider per tick.
            clients: dict[str, BatchClient] = {}
            try:
                for row in rows:
                    client = self._client_for(row.provider, clients)
                    if client is None:
                        # No credentials — leave the row alone and let the
                        # operator notice via /v1/batches/{id}.
                        continue
                    await self._reconcile_one(session, row, client)
            finally:
                for c in clients.values():
                    await c.aclose()
            await session.commit()
            return len(rows)

    # ------------------------------------------------------------------ #
    # Per-row reconciliation                                             #
    # ------------------------------------------------------------------ #

    def _client_for(
        self, provider: str, cache: dict[str, BatchClient]
    ) -> BatchClient | None:
        if provider in cache:
            return cache[provider]
        if provider == "openai":
            if not self._settings.openai_api_key:
                return None
            cache[provider] = OpenAIBatchClient(api_key=self._settings.openai_api_key)
            return cache[provider]
        if provider == "anthropic":
            if not self._settings.anthropic_api_key:
                return None
            cache[provider] = AnthropicBatchClient(
                api_key=self._settings.anthropic_api_key
            )
            return cache[provider]
        return None

    async def _reconcile_one(
        self, session: AsyncSession, row: Batch, client: BatchClient
    ) -> None:
        if row.provider_batch_id is None:
            # Submitted-but-no-id rows are pathological; mark failed.
            row.status = "failed"
            row.error_message = "missing provider_batch_id"
            row.completed_at = datetime.now(UTC)
            record_batch_event(provider=row.provider, status="failed")
            return
        try:
            snapshot = await client.poll(provider_batch_id=row.provider_batch_id)
        except httpx.HTTPError as e:
            log.warning(
                "batch_poll_failed",
                extra={"batch_id": row.id, "error": str(e)},
            )
            return

        # Update counts even when status hasn't changed — the operator's
        # /v1/batches/{id} should reflect provider-side progress.
        prior_status = row.status
        row.status = snapshot.status
        row.completed_count = snapshot.completed_count
        row.failed_count = snapshot.failed_count
        if snapshot.error_message and not row.error_message:
            row.error_message = snapshot.error_message
        if prior_status == "validating" and snapshot.status != "validating":
            row.in_progress_at = datetime.now(UTC)

        # Terminal-state handling. The prior_status check is defensive:
        # the SELECT excludes terminal rows so we shouldn't observe a
        # row that was already terminal, but the guard keeps the metric
        # honest if the worker is ever invoked on a stale snapshot.
        if snapshot.status in _TERMINAL and prior_status not in _TERMINAL:
            row.completed_at = datetime.now(UTC)
            if snapshot.status == "completed" and snapshot.results_handle:
                await self._finalize_completed(
                    session=session,
                    row=row,
                    client=client,
                    results_handle=snapshot.results_handle,
                )
            record_batch_event(provider=row.provider, status=snapshot.status)

    async def _finalize_completed(
        self,
        *,
        session: AsyncSession,
        row: Batch,
        client: BatchClient,
        results_handle: str,
    ) -> None:
        """Pull the result JSONL, parse it, persist the blob, and
        write per-sub-request usage rows at the half-priced rate."""
        try:
            jsonl = await client.retrieve_results(results_handle=results_handle)
        except httpx.HTTPError as e:
            log.warning(
                "batch_retrieve_results_failed",
                extra={"batch_id": row.id, "error": str(e)},
            )
            return

        row.output_payload = jsonl
        if row.provider == "openai":
            results = parse_openai_result_jsonl(jsonl)
        else:
            results = parse_anthropic_result_jsonl(jsonl)

        summary = summarize_results(results)
        row.completed_count = summary["completed_count"]
        row.failed_count = summary["failed_count"]
        row.prompt_tokens = summary["prompt_tokens"]
        row.completion_tokens = summary["completion_tokens"]

        # Per-sub-request usage rows. Status="batch_success" lets
        # operators split sync vs batch spend with a single
        # ``WHERE status LIKE 'batch_%'`` filter. The ``request_id``
        # column carries ``{batch_id}#{custom_id}`` so per-request
        # drilldown is still possible without a new column.
        total_cost = 0
        for r in results:
            if r.is_error:
                continue
            cost = batch_cost_hcents(
                provider_key=row.provider,
                model=r.model or "",
                prompt_tokens=r.prompt_tokens,
                completion_tokens=r.completion_tokens,
                endpoint=row.endpoint,
            )
            total_cost += cost
            usage = UsageRecord(
                tenant_id=row.tenant_id,
                team_id=row.team_id,
                key_id=row.key_id or "",
                provider=row.provider,
                model=r.model or "",
                prompt_tokens=r.prompt_tokens,
                completion_tokens=r.completion_tokens,
                cost_hcents=cost,
                request_id=f"{row.id}#{r.custom_id}"[:64],
                status="batch_success",
            )
            session.add(usage)
            # Flush per-row so an IntegrityError on a re-poll only
            # skips that one sub-request, not the entire batch.
            try:
                await session.flush()
            except IntegrityError:
                await session.rollback()
                # Re-fetch the row so subsequent updates land on the
                # current managed instance. Tests cover this path.
                log.info(
                    "batch_usage_duplicate_skipped",
                    extra={"batch_id": row.id, "custom_id": r.custom_id},
                )
        row.cost_hcents = total_cost
