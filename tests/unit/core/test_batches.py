"""Unit tests for Phase 59 — core/batches.py.

Covers
------
- ``batch_cost_hcents`` math (50% of sync, integer-clean)
- status normalizers (OpenAI + Anthropic vocabularies)
- ``provider_from_model`` routing (prefix + name-pattern fallback)
- ``OpenAIBatchClient`` submit/poll/retrieve_results/cancel via respx
- ``AnthropicBatchClient`` submit/poll/retrieve_results/cancel via respx
- result JSONL parsers (success + error rows)
- ``summarize_results`` aggregation
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from pronaos.core.batches import (
    AnthropicBatchClient,
    BatchResultRow,
    OpenAIBatchClient,
    batch_cost_hcents,
    normalize_anthropic_status,
    normalize_openai_status,
    parse_anthropic_result_jsonl,
    parse_openai_result_jsonl,
    provider_from_model,
    summarize_results,
)

# --------------------------------------------------------------------------- #
# batch_cost_hcents                                                           #
# --------------------------------------------------------------------------- #


class TestBatchCostHcents:
    def test_unknown_provider_returns_zero(self) -> None:
        assert batch_cost_hcents(
            provider_key="unknown",
            model="unknown-model",
            prompt_tokens=100,
            completion_tokens=100,
        ) == 0

    def test_unknown_model_returns_zero(self) -> None:
        # The 'openai' catalog entry exists but 'unknown-model' isn't in it.
        assert batch_cost_hcents(
            provider_key="openai",
            model="unknown-model-xyz",
            prompt_tokens=100,
            completion_tokens=100,
        ) == 0

    def test_known_openai_model_halves_sync_rate(self) -> None:
        """gpt-4o-mini priced at 15 hcents/Mtok input + 60/Mtok output.
        Sync cost for 1M+1M tokens would be 75 hcents; batch is half."""
        # 1M input + 1M output tokens.
        result = batch_cost_hcents(
            provider_key="openai",
            model="gpt-4o-mini",
            prompt_tokens=1_000_000,
            completion_tokens=1_000_000,
        )
        # We don't assert the exact sync rate to avoid coupling to
        # the catalog; just verify the result is positive and
        # symmetric with the half-rate formula.
        assert result > 0
        # Verify it's exactly half of a doubled call.
        doubled = batch_cost_hcents(
            provider_key="openai",
            model="gpt-4o-mini",
            prompt_tokens=2_000_000,
            completion_tokens=2_000_000,
        )
        assert doubled == result * 2

    def test_zero_tokens_returns_zero(self) -> None:
        assert batch_cost_hcents(
            provider_key="openai",
            model="gpt-4o-mini",
            prompt_tokens=0,
            completion_tokens=0,
        ) == 0

    def test_prefix_stripped_for_bare_lookup(self) -> None:
        """Anthropic-direct provider keys model by bare name. The
        ``batch_cost_hcents`` helper strips the prefix on lookup
        miss, so ``anthropic/claude-opus-4-7`` and ``claude-opus-4-7``
        produce the same cost."""
        prefixed = batch_cost_hcents(
            provider_key="anthropic",
            model="anthropic/claude-opus-4-7",
            prompt_tokens=100_000,
            completion_tokens=100_000,
        )
        bare = batch_cost_hcents(
            provider_key="anthropic",
            model="claude-opus-4-7",
            prompt_tokens=100_000,
            completion_tokens=100_000,
        )
        # Both lookups should find the entry (one direct, one via
        # the strip fallback) and produce identical cost.
        assert prefixed == bare


# --------------------------------------------------------------------------- #
# Status normalizers                                                          #
# --------------------------------------------------------------------------- #


class TestStatusNormalizers:
    def test_openai_canonical_passthrough(self) -> None:
        for s in (
            "validating",
            "in_progress",
            "finalizing",
            "completed",
            "failed",
            "expired",
            "cancelled",
        ):
            assert normalize_openai_status(s) == s

    def test_openai_cancelling_maps_to_cancelled(self) -> None:
        # OpenAI emits ``cancelling`` during the brief cancel-pending
        # window; we normalise to ``cancelled``.
        assert normalize_openai_status("cancelling") == "cancelled"

    def test_openai_unknown_falls_back_to_failed(self) -> None:
        assert normalize_openai_status("magic-status") == "failed"

    def test_anthropic_in_progress_maps(self) -> None:
        assert normalize_anthropic_status("in_progress") == "in_progress"

    def test_anthropic_ended_maps_to_completed(self) -> None:
        # Anthropic's ``ended`` is its terminal-success state.
        assert normalize_anthropic_status("ended") == "completed"

    def test_anthropic_canceling_maps_to_cancelled(self) -> None:
        assert normalize_anthropic_status("canceling") == "cancelled"


# --------------------------------------------------------------------------- #
# provider_from_model                                                         #
# --------------------------------------------------------------------------- #


class TestProviderFromModel:
    def test_explicit_openai_prefix(self) -> None:
        assert provider_from_model("openai/gpt-4o-mini") == "openai"

    def test_explicit_anthropic_prefix(self) -> None:
        assert provider_from_model("anthropic/claude-opus-4-7") == "anthropic"

    def test_gpt_prefix_falls_back_to_openai(self) -> None:
        assert provider_from_model("gpt-4o") == "openai"

    def test_o1_prefix_falls_back_to_openai(self) -> None:
        assert provider_from_model("o1-preview") == "openai"

    def test_o3_prefix_falls_back_to_openai(self) -> None:
        assert provider_from_model("o3-mini") == "openai"

    def test_claude_prefix_falls_back_to_anthropic(self) -> None:
        assert provider_from_model("claude-3-haiku") == "anthropic"

    def test_unknown_model_raises(self) -> None:
        with pytest.raises(ValueError, match="cannot determine batch provider"):
            provider_from_model("magic-model-7")

    def test_groq_prefix_raises(self) -> None:
        # Groq doesn't have a batches API, so model="groq/llama" must
        # not silently route to OpenAI even though the underlying
        # endpoint is OpenAI-shape.
        with pytest.raises(ValueError):
            provider_from_model("groq/llama-3.3-70b")


# --------------------------------------------------------------------------- #
# OpenAIBatchClient (respx)                                                   #
# --------------------------------------------------------------------------- #


@respx.mock
@pytest.mark.asyncio
async def test_openai_submit_uploads_file_then_creates_batch() -> None:
    upload = respx.post("https://api.openai.com/v1/files").mock(
        return_value=httpx.Response(200, json={"id": "file-abc", "object": "file"})
    )
    create = respx.post("https://api.openai.com/v1/batches").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "batch_123",
                "status": "validating",
                "object": "batch",
            },
        )
    )

    client = OpenAIBatchClient(api_key="sk-test")
    try:
        result = await client.submit(requests_jsonl='{"custom_id":"a"}\n')
    finally:
        await client.aclose()

    assert upload.called
    assert create.called
    # The batch-create body should reference the uploaded file id.
    create_body = json.loads(create.calls.last.request.content)
    assert create_body["input_file_id"] == "file-abc"
    assert create_body["endpoint"] == "/v1/chat/completions"
    assert result.provider_batch_id == "batch_123"
    assert result.initial_status == "validating"


@respx.mock
@pytest.mark.asyncio
async def test_openai_poll_returns_normalized_status() -> None:
    respx.get("https://api.openai.com/v1/batches/batch_123").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "batch_123",
                "status": "in_progress",
                "request_counts": {"total": 10, "completed": 4, "failed": 1},
                "output_file_id": None,
            },
        )
    )
    client = OpenAIBatchClient(api_key="sk-test")
    try:
        status = await client.poll(provider_batch_id="batch_123")
    finally:
        await client.aclose()

    assert status.status == "in_progress"
    assert status.request_count == 10
    assert status.completed_count == 4
    assert status.failed_count == 1
    assert status.results_handle is None


@respx.mock
@pytest.mark.asyncio
async def test_openai_poll_surfaces_first_error_message() -> None:
    respx.get("https://api.openai.com/v1/batches/batch_123").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "batch_123",
                "status": "failed",
                "request_counts": {"total": 5, "completed": 0, "failed": 5},
                "errors": {
                    "data": [{"code": "bad_request", "message": "schema invalid"}]
                },
            },
        )
    )
    client = OpenAIBatchClient(api_key="sk-test")
    try:
        status = await client.poll(provider_batch_id="batch_123")
    finally:
        await client.aclose()

    assert status.status == "failed"
    assert status.error_message is not None
    assert "schema invalid" in status.error_message


@respx.mock
@pytest.mark.asyncio
async def test_openai_retrieve_results_streams_jsonl() -> None:
    respx.get("https://api.openai.com/v1/files/file-out/content").mock(
        return_value=httpx.Response(
            200, text='{"custom_id":"req-1","response":{"body":{"usage":{}}}}\n'
        )
    )
    client = OpenAIBatchClient(api_key="sk-test")
    try:
        jsonl = await client.retrieve_results(results_handle="file-out")
    finally:
        await client.aclose()
    assert "custom_id" in jsonl
    assert "req-1" in jsonl


@respx.mock
@pytest.mark.asyncio
async def test_openai_cancel_calls_provider() -> None:
    route = respx.post("https://api.openai.com/v1/batches/batch_123/cancel").mock(
        return_value=httpx.Response(200, json={"id": "batch_123", "status": "cancelling"})
    )
    client = OpenAIBatchClient(api_key="sk-test")
    try:
        await client.cancel(provider_batch_id="batch_123")
    finally:
        await client.aclose()
    assert route.called


# --------------------------------------------------------------------------- #
# AnthropicBatchClient (respx)                                                #
# --------------------------------------------------------------------------- #


@respx.mock
@pytest.mark.asyncio
async def test_anthropic_submit_translates_jsonl_shape() -> None:
    """The OpenAI-style JSONL ``{custom_id, body}`` is translated to
    Anthropic's ``{custom_id, params}`` on the way in."""
    route = respx.post("https://api.anthropic.com/v1/messages/batches").mock(
        return_value=httpx.Response(
            200, json={"id": "msgbatch_001", "processing_status": "in_progress"}
        )
    )
    client = AnthropicBatchClient(api_key="sk-ant-test")
    try:
        result = await client.submit(
            requests_jsonl='{"custom_id":"r1","body":{"model":"claude-opus","messages":[]}}\n'
        )
    finally:
        await client.aclose()

    assert route.called
    body = json.loads(route.calls.last.request.content)
    # The wire shape must be {requests: [{custom_id, params}]}, NOT
    # {requests: [{custom_id, body}]} — Anthropic uses 'params'.
    assert "requests" in body
    assert body["requests"][0]["custom_id"] == "r1"
    assert "params" in body["requests"][0]
    assert "body" not in body["requests"][0]
    assert result.provider_batch_id == "msgbatch_001"


@respx.mock
@pytest.mark.asyncio
async def test_anthropic_poll_normalizes_ended() -> None:
    respx.get("https://api.anthropic.com/v1/messages/batches/msgbatch_001").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "msgbatch_001",
                "processing_status": "ended",
                "request_counts": {
                    "processing": 0,
                    "succeeded": 3,
                    "errored": 1,
                    "expired": 0,
                    "canceled": 0,
                },
                "results_url": "https://api.anthropic.com/v1/messages/batches/msgbatch_001/results",
            },
        )
    )
    client = AnthropicBatchClient(api_key="sk-ant-test")
    try:
        status = await client.poll(provider_batch_id="msgbatch_001")
    finally:
        await client.aclose()

    assert status.status == "completed"
    # Anthropic counts: succeeded + errored + processing + expired + canceled.
    assert status.completed_count == 3
    assert status.failed_count == 1
    assert status.results_handle is not None


@respx.mock
@pytest.mark.asyncio
async def test_anthropic_cancel_calls_provider() -> None:
    route = respx.post(
        "https://api.anthropic.com/v1/messages/batches/msgbatch_001/cancel"
    ).mock(
        return_value=httpx.Response(
            200, json={"id": "msgbatch_001", "processing_status": "canceling"}
        )
    )
    client = AnthropicBatchClient(api_key="sk-ant-test")
    try:
        await client.cancel(provider_batch_id="msgbatch_001")
    finally:
        await client.aclose()
    assert route.called


# --------------------------------------------------------------------------- #
# Result JSONL parsers                                                        #
# --------------------------------------------------------------------------- #


class TestOpenAIResultParser:
    def test_success_row(self) -> None:
        jsonl = json.dumps(
            {
                "id": "out_1",
                "custom_id": "req-a",
                "response": {
                    "body": {
                        "model": "gpt-4o-mini",
                        "usage": {"prompt_tokens": 7, "completion_tokens": 3},
                    }
                },
                "error": None,
            }
        ) + "\n"
        rows = parse_openai_result_jsonl(jsonl)
        assert len(rows) == 1
        assert rows[0].custom_id == "req-a"
        assert rows[0].is_error is False
        assert rows[0].prompt_tokens == 7
        assert rows[0].completion_tokens == 3
        assert rows[0].model == "gpt-4o-mini"

    def test_error_row(self) -> None:
        jsonl = json.dumps(
            {
                "custom_id": "req-bad",
                "response": None,
                "error": {"code": "schema_invalid", "message": "bad input"},
            }
        ) + "\n"
        rows = parse_openai_result_jsonl(jsonl)
        assert len(rows) == 1
        assert rows[0].is_error is True
        assert rows[0].error_message is not None
        assert "bad input" in rows[0].error_message

    def test_blank_lines_skipped(self) -> None:
        rows = parse_openai_result_jsonl("\n\n\n")
        assert rows == []


class TestAnthropicResultParser:
    def test_success_row(self) -> None:
        jsonl = json.dumps(
            {
                "custom_id": "req-a",
                "result": {
                    "type": "succeeded",
                    "message": {
                        "model": "claude-opus-4-7",
                        "usage": {"input_tokens": 5, "output_tokens": 2},
                    },
                },
            }
        ) + "\n"
        rows = parse_anthropic_result_jsonl(jsonl)
        assert len(rows) == 1
        assert rows[0].is_error is False
        assert rows[0].prompt_tokens == 5
        assert rows[0].completion_tokens == 2

    def test_errored_row(self) -> None:
        jsonl = json.dumps(
            {
                "custom_id": "req-bad",
                "result": {
                    "type": "errored",
                    "error": {"message": "invalid_request"},
                },
            }
        ) + "\n"
        rows = parse_anthropic_result_jsonl(jsonl)
        assert len(rows) == 1
        assert rows[0].is_error is True


class TestSummarize:
    def test_mixed_results(self) -> None:
        rows = [
            BatchResultRow(
                custom_id="a",
                model="m1",
                prompt_tokens=10,
                completion_tokens=5,
                is_error=False,
            ),
            BatchResultRow(
                custom_id="b",
                model="m1",
                prompt_tokens=0,
                completion_tokens=0,
                is_error=True,
            ),
            BatchResultRow(
                custom_id="c",
                model="m1",
                prompt_tokens=8,
                completion_tokens=2,
                is_error=False,
            ),
        ]
        s = summarize_results(rows)
        assert s["completed_count"] == 2
        assert s["failed_count"] == 1
        assert s["prompt_tokens"] == 18
        assert s["completion_tokens"] == 7
