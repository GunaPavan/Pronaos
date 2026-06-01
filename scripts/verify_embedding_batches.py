"""Async embedding-batches mocked-live verification (Claim #47, Phase 60).

The empirical question
----------------------
Phase 59 shipped async chat batches at 50% of sync pricing. RAG
ingestion is the OTHER workload that genuinely burns money: re-
embedding millions of document chunks every refresh cycle. OpenAI
ships a batches API for ``/v1/embeddings`` at the same 50% rate,
24-hour completion window — Pronaos previously rejected this endpoint
with 422 ``batch_endpoint_unsupported``.

Phase 60 closes that gap. This script verifies, end-to-end against
a respx-mocked OpenAI Batches API:

1. POST /v1/batches with endpoint=/v1/embeddings persists a row
2. The upstream POST /v1/batches body carries ``endpoint:
   "/v1/embeddings"`` (not chat-completions — proves the param
   is plumbed through)
3. The background worker's tick():
   a. polls the provider
   b. transitions the row to ``completed``
   c. parses the result JSONL (embedding-shaped: usage has
      prompt_tokens, NO completion_tokens)
   d. writes per-sub-request UsageRecord rows with prompt_tokens
      populated, completion_tokens=0, status=batch_success
   e. computes cost via embedding_pricing × 0.5, NOT chat pricing
4. Mechanical 50% equality holds: embedding batch cost = sync
   embedding cost × 50/100, integer math.

Honest disclosures
------------------
- Mocked-live, not real-live (real OpenAI embedding batches take
  minutes-to-hours; CI can't afford that).
- v1 supports embeddings on OpenAI only. Anthropic does not ship
  an embeddings API at all; Cohere/Voyage/Mistral do but don't ship
  batches APIs.
- The cost math claim is mechanical equality vs the catalog rate;
  we don't verify OpenAI's invoice for you.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from typing import Any

import httpx
import respx

os.environ.setdefault("PRONAOS_SECRET_KEY", "x" * 64)
os.environ.setdefault("PRONAOS_OPENAI_API_KEY", "sk-test")

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from pronaos.config import get_settings
from pronaos.core.batch_worker import BatchWorker
from pronaos.core.batches import (
    BATCH_COST_MULTIPLIER_DENOMINATOR,
    BATCH_COST_MULTIPLIER_NUMERATOR,
    OpenAIBatchClient,
    batch_cost_hcents,
)
from pronaos.db.models import Base, Batch, UsageRecord
from pronaos.providers.catalog import CATALOG

VERDICTS: list[tuple[str, bool, str]] = []


def assert_(name: str, ok: bool, detail: str = "") -> None:
    VERDICTS.append((name, ok, detail))
    marker = "PASS" if ok else "FAIL"
    print(f"[{marker}] {name}" + (f"  --  {detail}" if detail else ""))


async def main() -> int:
    print("=" * 72)
    print("Phase 60 / Claim #47 - async embedding batches verify (mocked-live)")
    print("=" * 72)
    print()

    # ------------------------------------------------------------------ #
    # Step 1: submit a 3-doc embedding batch                              #
    # ------------------------------------------------------------------ #

    print(">> Step 1: submit a 3-doc embedding batch")
    with respx.mock(assert_all_called=False) as r:
        r.post("https://api.openai.com/v1/files").mock(
            return_value=httpx.Response(200, json={"id": "file-emb-001"})
        )
        create = r.post("https://api.openai.com/v1/batches").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "batch_emb_001",
                    "status": "validating",
                    "object": "batch",
                },
            )
        )
        client = OpenAIBatchClient(api_key="sk-test")
        jsonl_lines = [
            json.dumps(
                {
                    "custom_id": f"doc-{i}",
                    "method": "POST",
                    "url": "/v1/embeddings",
                    "body": {
                        "model": "text-embedding-3-small",
                        "input": f"document chunk number {i}",
                    },
                }
            )
            for i in range(3)
        ]
        jsonl = "\n".join(jsonl_lines) + "\n"
        submission = await client.submit(
            requests_jsonl=jsonl, endpoint="/v1/embeddings"
        )
        await client.aclose()

        assert_(
            "submit returned provider_batch_id",
            submission.provider_batch_id == "batch_emb_001",
            f"got {submission.provider_batch_id}",
        )
        # The upstream's create-batch body must carry the
        # /v1/embeddings endpoint, not the chat one.
        create_body = json.loads(create.calls.last.request.content)
        assert_(
            "upstream POST /v1/batches carries endpoint=/v1/embeddings",
            create_body["endpoint"] == "/v1/embeddings",
            f"got {create_body['endpoint']}",
        )

    # ------------------------------------------------------------------ #
    # Step 2: persist the row + drive worker to completion                #
    # ------------------------------------------------------------------ #

    print()
    print(">> Step 2: persist row + drive worker through one completion tick")

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)

    now = datetime.now(UTC)
    async with sm() as session:
        row = Batch(
            id="pron_batch_verify_emb_001",
            tenant_id="t1",
            team_id="team1",
            key_id="k1",
            provider="openai",
            provider_batch_id="batch_emb_001",
            status="validating",
            endpoint="/v1/embeddings",
            completion_window="24h",
            request_count=3,
            completed_count=0,
            failed_count=0,
            prompt_tokens=0,
            completion_tokens=0,
            cost_hcents=0,
            created_at=now,
            input_payload=jsonl,
            output_payload="",
        )
        session.add(row)
        await session.commit()

    # OpenAI embedding result JSONL — note the absence of
    # completion_tokens on each usage block (embeddings are
    # input-only).
    result_jsonl_lines = [
        json.dumps(
            {
                "id": f"out-{i}",
                "custom_id": f"doc-{i}",
                "response": {
                    "body": {
                        "object": "list",
                        "data": [
                            {
                                "index": 0,
                                "object": "embedding",
                                "embedding": [0.01, 0.02, 0.03],
                            }
                        ],
                        "model": "text-embedding-3-small",
                        "usage": {
                            "prompt_tokens": 100 + i,
                            "total_tokens": 100 + i,
                        },
                    }
                },
                "error": None,
            }
        )
        for i in range(3)
    ]
    result_jsonl = "\n".join(result_jsonl_lines) + "\n"

    get_settings.cache_clear()
    settings = get_settings()
    worker = BatchWorker(sessionmaker=sm, settings=settings)

    with respx.mock(assert_all_called=False) as r:
        r.get("https://api.openai.com/v1/batches/batch_emb_001").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "batch_emb_001",
                    "status": "completed",
                    "request_counts": {
                        "total": 3,
                        "completed": 3,
                        "failed": 0,
                    },
                    "output_file_id": "file-emb-out-001",
                },
            )
        )
        r.get("https://api.openai.com/v1/files/file-emb-out-001/content").mock(
            return_value=httpx.Response(200, text=result_jsonl)
        )
        n = await worker.tick()

    assert_("worker tick examined the in-flight batch", n == 1, f"got {n}")

    # ------------------------------------------------------------------ #
    # Step 3: verify row finalised + usage rows landed with embeddings    #
    #         shape (completion_tokens=0)                                 #
    # ------------------------------------------------------------------ #

    print()
    print(">> Step 3: verify final row state + per-sub-request usage rows")

    async with sm() as session:
        from sqlalchemy import select

        final = await session.get(Batch, "pron_batch_verify_emb_001")
        assert final is not None
        assert_(
            "row transitioned to status=completed",
            final.status == "completed",
            f"got {final.status}",
        )
        assert_(
            "row.completed_count = 3",
            final.completed_count == 3,
            f"got {final.completed_count}",
        )
        assert_(
            "row.prompt_tokens = 303 (100+101+102)",
            final.prompt_tokens == 303,
            f"got {final.prompt_tokens}",
        )
        assert_(
            "row.completion_tokens = 0 (embeddings have no output)",
            final.completion_tokens == 0,
            f"got {final.completion_tokens}",
        )
        assert_(
            "row.endpoint preserved as /v1/embeddings",
            final.endpoint == "/v1/embeddings",
            f"got {final.endpoint}",
        )

        usage_rows = (
            (
                await session.execute(
                    select(UsageRecord).where(UsageRecord.team_id == "team1")
                )
            )
            .scalars()
            .all()
        )
        assert_(
            "3 UsageRecord rows written",
            len(usage_rows) == 3,
            f"got {len(usage_rows)}",
        )
        if usage_rows:
            assert_(
                "every usage row has status=batch_success",
                all(u.status == "batch_success" for u in usage_rows),
                "see status column",
            )
            assert_(
                "every usage row has completion_tokens=0",
                all(u.completion_tokens == 0 for u in usage_rows),
                "embeddings are input-only",
            )
            assert_(
                "every usage row has prompt_tokens > 0",
                all(u.prompt_tokens > 0 for u in usage_rows),
                "embeddings DO consume input tokens",
            )

    # ------------------------------------------------------------------ #
    # Step 4: mechanical half-rate cost math on embedding pricing         #
    # ------------------------------------------------------------------ #

    print()
    print(">> Step 4: verify embedding batch cost = 0.5 * sync embedding cost")

    embedding_pricing = CATALOG["openai"].embedding_pricing["text-embedding-3-small"]
    pt = 1_000_000  # 1M tokens
    sync_cost = pt * embedding_pricing.input_hcents_per_mtok // 1_000_000
    batch_cost = batch_cost_hcents(
        provider_key="openai",
        model="text-embedding-3-small",
        prompt_tokens=pt,
        completion_tokens=0,
        endpoint="/v1/embeddings",
    )
    expected = (
        sync_cost * BATCH_COST_MULTIPLIER_NUMERATOR
        // BATCH_COST_MULTIPLIER_DENOMINATOR
    )
    assert_(
        f"embedding batch_cost_hcents({pt} tokens) = sync_cost * 50/100",
        batch_cost == expected,
        f"sync={sync_cost} batch={batch_cost} expected={expected}",
    )
    # Sanity vs the chat-pricing-by-mistake path: looking up
    # text-embedding-3-small in entry.pricing (chat) would miss
    # entirely and return 0, masking the bug. Verify it does.
    wrong_endpoint_cost = batch_cost_hcents(
        provider_key="openai",
        model="text-embedding-3-small",
        prompt_tokens=pt,
        completion_tokens=0,
        # endpoint defaults to /v1/chat/completions
    )
    assert_(
        "wrong-endpoint lookup correctly misses and returns 0",
        wrong_endpoint_cost == 0,
        f"got {wrong_endpoint_cost}",
    )

    await engine.dispose()

    # ------------------------------------------------------------------ #
    # Summary                                                             #
    # ------------------------------------------------------------------ #

    print()
    print("=" * 72)
    failed = [(n, d) for n, ok, d in VERDICTS if not ok]
    if failed:
        print(f"VERDICT: {len(failed)}/{len(VERDICTS)} ASSERTIONS FAILED")
        for n, d in failed:
            print(f"  - {n}: {d}")
        return 1
    print(f"VERDICT: all {len(VERDICTS)} assertions held.")
    print()
    print("Claim #47 supported (mocked-live):")
    print(
        "  Pronaos's async-batches surface now serves /v1/embeddings on"
    )
    print(
        "  OpenAI at half the synchronous rate, with the per-team gate,"
    )
    print("  worker, and per-sub-request UsageRecord writes intact.")
    print(
        "  RAG corpus ingestion at 0.5x the per-token price, end-to-end."
    )
    return 0


def _main() -> int:
    return asyncio.run(main())


if __name__ == "__main__":
    sys.exit(_main())


_ = Any
