"""Phase 60 — async embedding batches tests for core/batches.py.

Covers
------
- ``batch_cost_hcents`` reads ``embedding_pricing`` when endpoint=/v1/embeddings
- ``provider_from_model`` maps bare ``text-embedding-*`` names to openai
- ``OpenAIBatchClient.submit`` sends endpoint=/v1/embeddings through to
  the upstream create-batch body when asked
- ``AnthropicBatchClient.submit`` rejects /v1/embeddings (Anthropic doesn't
  expose an embeddings API)
- ``parse_openai_result_jsonl`` handles the embedding-shaped result row
  (completion_tokens absent → 0)
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from pronaos.core.batches import (
    AnthropicBatchClient,
    OpenAIBatchClient,
    batch_cost_hcents,
    parse_openai_result_jsonl,
    provider_from_model,
)


class TestBatchCostHcentsEmbeddings:
    def test_endpoint_chat_uses_chat_pricing(self) -> None:
        """Sanity: existing chat path still uses entry.pricing."""
        chat = batch_cost_hcents(
            provider_key="openai",
            model="gpt-4o-mini",
            prompt_tokens=1_000_000,
            completion_tokens=0,
        )
        assert chat > 0

    def test_endpoint_embeddings_uses_embedding_pricing(self) -> None:
        """text-embedding-3-small is in entry.embedding_pricing, NOT
        in entry.pricing. Without the endpoint discriminator the
        lookup would miss and return 0."""
        # 1M input tokens at $0.02/Mtok (= 2000 hcents/Mtok) sync,
        # halved → 1000 hcents.
        cost = batch_cost_hcents(
            provider_key="openai",
            model="text-embedding-3-small",
            prompt_tokens=1_000_000,
            completion_tokens=0,
            endpoint="/v1/embeddings",
        )
        assert cost == 1000

    def test_embeddings_wrong_endpoint_misses(self) -> None:
        """A caller that forgets to pass endpoint=/v1/embeddings gets
        0 — the embedding model isn't in entry.pricing."""
        cost = batch_cost_hcents(
            provider_key="openai",
            model="text-embedding-3-small",
            prompt_tokens=1_000_000,
            completion_tokens=0,
            # Defaults to /v1/chat/completions.
        )
        assert cost == 0

    def test_unknown_embedding_model_returns_zero(self) -> None:
        cost = batch_cost_hcents(
            provider_key="openai",
            model="text-embedding-nonexistent",
            prompt_tokens=1000,
            completion_tokens=0,
            endpoint="/v1/embeddings",
        )
        assert cost == 0

    def test_embeddings_completion_tokens_ignored(self) -> None:
        """Embedding pricing has output_hcents_per_mtok=0, so even
        if a buggy result row sneaks in non-zero completion_tokens
        the cost math doesn't double-bill."""
        cost = batch_cost_hcents(
            provider_key="openai",
            model="text-embedding-3-small",
            prompt_tokens=0,
            completion_tokens=1_000_000,
            endpoint="/v1/embeddings",
        )
        assert cost == 0


class TestProviderFromModelEmbeddings:
    def test_text_embedding_3_small_maps_to_openai(self) -> None:
        assert provider_from_model("text-embedding-3-small") == "openai"

    def test_text_embedding_3_large_maps_to_openai(self) -> None:
        assert provider_from_model("text-embedding-3-large") == "openai"

    def test_explicit_openai_prefix_still_works(self) -> None:
        assert provider_from_model("openai/text-embedding-3-small") == "openai"

    def test_voyage_embedding_still_rejected(self) -> None:
        """Voyage has embeddings but no batches API. Without an
        explicit prefix the name shouldn't route to OpenAI."""
        with pytest.raises(ValueError):
            provider_from_model("voyage-3-large")


@respx.mock
@pytest.mark.asyncio
async def test_openai_submit_passes_endpoint_to_upstream() -> None:
    """OpenAIBatchClient.submit must pass endpoint into the
    POST /v1/batches body — without this the upstream rejects
    embeddings batches as malformed."""
    respx.post("https://api.openai.com/v1/files").mock(
        return_value=httpx.Response(200, json={"id": "file-emb-1"})
    )
    create = respx.post("https://api.openai.com/v1/batches").mock(
        return_value=httpx.Response(200, json={"id": "batch_emb_1", "status": "validating"})
    )
    client = OpenAIBatchClient(api_key="sk-test")
    try:
        await client.submit(
            requests_jsonl='{"custom_id":"r1","body":{"model":"text-embedding-3-small","input":"hi"}}\n',
            endpoint="/v1/embeddings",
        )
    finally:
        await client.aclose()
    create_body = json.loads(create.calls.last.request.content)
    assert create_body["endpoint"] == "/v1/embeddings"


@respx.mock
@pytest.mark.asyncio
async def test_openai_submit_defaults_to_chat() -> None:
    """No endpoint kwarg passed → defaults to /v1/chat/completions
    (backwards-compat with Phase 59)."""
    respx.post("https://api.openai.com/v1/files").mock(
        return_value=httpx.Response(200, json={"id": "file-chat-1"})
    )
    create = respx.post("https://api.openai.com/v1/batches").mock(
        return_value=httpx.Response(200, json={"id": "batch_chat_1", "status": "validating"})
    )
    client = OpenAIBatchClient(api_key="sk-test")
    try:
        await client.submit(
            requests_jsonl='{"custom_id":"r1","body":{"model":"gpt-4o-mini"}}\n',
        )
    finally:
        await client.aclose()
    create_body = json.loads(create.calls.last.request.content)
    assert create_body["endpoint"] == "/v1/chat/completions"


@pytest.mark.asyncio
async def test_anthropic_submit_rejects_embeddings() -> None:
    """Anthropic doesn't expose an embeddings API — passing
    endpoint=/v1/embeddings to AnthropicBatchClient.submit must
    raise rather than silently mis-route."""
    client = AnthropicBatchClient(api_key="sk-ant-test")
    try:
        with pytest.raises(ValueError, match="/v1/chat/completions"):
            await client.submit(
                requests_jsonl='{"custom_id":"r1","body":{}}\n',
                endpoint="/v1/embeddings",
            )
    finally:
        await client.aclose()


class TestEmbeddingResultParser:
    def test_embedding_success_row(self) -> None:
        """OpenAI's embedding result body has no completion_tokens —
        the existing parser already returns 0 for it (via the
        ``or 0`` fallback). Verify the contract."""
        jsonl = (
            json.dumps(
                {
                    "id": "out_emb_1",
                    "custom_id": "doc-1",
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
                                "prompt_tokens": 7,
                                "total_tokens": 7,
                                # NOTE: no completion_tokens field in
                                # the OpenAI embedding response shape.
                            },
                        }
                    },
                    "error": None,
                }
            )
            + "\n"
        )
        rows = parse_openai_result_jsonl(jsonl)
        assert len(rows) == 1
        assert rows[0].is_error is False
        assert rows[0].prompt_tokens == 7
        assert rows[0].completion_tokens == 0
        assert rows[0].model == "text-embedding-3-small"
