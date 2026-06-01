"""Unit tests for the embedding provider adapters (Phase 31).

Three HTTP-backed adapters + one local adapter; each tested against a
mocked upstream via ``respx``. Tests assert:

- request shape matches the upstream's expected schema (OpenAI's
  ``input``, Cohere's ``texts``+``input_type``, Voyage's
  ``input``+``input_type``)
- response parsing extracts vectors in the correct order
- usage extraction handles each provider's response shape
- cost math is per-Mtok-input
- error classes (auth, rate limit, network, upstream 5xx) map
  to the same shared error hierarchy as chat
"""

from __future__ import annotations

import httpx
import pytest
import respx

from pronaos.providers.base import (
    AuthError,
    ProviderError,
    RateLimitError,
)
from pronaos.providers.embeddings import (
    CohereEmbeddingProvider,
    EmbeddingProviderRequest,
    OpenAICompatibleEmbeddingProvider,
    VoyageEmbeddingProvider,
    normalize_input_texts,
)
from pronaos.providers.openai_compat import Pricing

# --------------------------------------------------------------------------- #
# normalize_input_texts                                                       #
# --------------------------------------------------------------------------- #


class TestNormalize:
    def test_str_becomes_singleton_list(self) -> None:
        assert normalize_input_texts("hello") == ["hello"]

    def test_list_passes_through(self) -> None:
        assert normalize_input_texts(["a", "b"]) == ["a", "b"]

    def test_empty_string_stays_as_list_with_one(self) -> None:
        # An empty string is still ONE element — the API handler
        # decides whether to 400; the normaliser doesn't drop.
        assert normalize_input_texts("") == [""]


# --------------------------------------------------------------------------- #
# OpenAICompatibleEmbeddingProvider                                           #
# --------------------------------------------------------------------------- #


OPENAI_URL = "https://api.openai.com/v1/embeddings"


@pytest.fixture
def openai_provider() -> OpenAICompatibleEmbeddingProvider:
    return OpenAICompatibleEmbeddingProvider(
        provider_key="openai",
        base_url="https://api.openai.com/v1",
        api_key="test-key",
        pricing={
            "text-embedding-3-small": Pricing(
                input_hcents_per_mtok=2_000,
                output_hcents_per_mtok=0,
            ),
            "text-embedding-3-large": Pricing(
                input_hcents_per_mtok=13_000,
                output_hcents_per_mtok=0,
            ),
        },
    )


def _openai_response(
    *vectors: list[float], model: str = "text-embedding-3-small", prompt_tokens: int = 12
) -> dict:
    return {
        "object": "list",
        "data": [
            {"object": "embedding", "embedding": vec, "index": i} for i, vec in enumerate(vectors)
        ],
        "model": model,
        "usage": {"prompt_tokens": prompt_tokens, "total_tokens": prompt_tokens},
    }


class TestOpenAIShape:
    @respx.mock
    @pytest.mark.asyncio
    async def test_single_input_round_trip(
        self, openai_provider: OpenAICompatibleEmbeddingProvider
    ) -> None:
        route = respx.post(OPENAI_URL).mock(
            return_value=httpx.Response(200, json=_openai_response([0.1, 0.2, 0.3]))
        )
        req = EmbeddingProviderRequest(
            model="openai/text-embedding-3-small",
            input_texts=["Pronaos is a gateway"],
        )
        result = await openai_provider.embed(req)
        assert result.vectors == [[0.1, 0.2, 0.3]]
        assert result.prompt_tokens == 12
        # The model echoed back is the bare model — adapter does not
        # add the provider prefix on the return.
        assert "text-embedding-3-small" in result.model

        # Request body inspection — verify the adapter stripped the
        # provider prefix and used 'input' (not 'texts').
        request = route.calls[0].request
        assert b'"model":"text-embedding-3-small"' in request.content
        # OpenAI accepts ``input`` as a list or string. We always
        # send a list internally.
        assert b'"input":["Pronaos is a gateway"]' in request.content

    @respx.mock
    @pytest.mark.asyncio
    async def test_batched_input_preserves_order(
        self, openai_provider: OpenAICompatibleEmbeddingProvider
    ) -> None:
        # Mock returns vectors in REVERSE index order; adapter must
        # sort by index on the way back.
        payload = {
            "object": "list",
            "data": [
                {"object": "embedding", "embedding": [9.9], "index": 2},
                {"object": "embedding", "embedding": [0.0], "index": 0},
                {"object": "embedding", "embedding": [5.5], "index": 1},
            ],
            "model": "text-embedding-3-small",
            "usage": {"prompt_tokens": 30, "total_tokens": 30},
        }
        respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=payload))
        req = EmbeddingProviderRequest(
            model="openai/text-embedding-3-small",
            input_texts=["zero", "one", "two"],
        )
        result = await openai_provider.embed(req)
        assert result.vectors == [[0.0], [5.5], [9.9]]

    @respx.mock
    @pytest.mark.asyncio
    async def test_dimensions_flag_passed_through(
        self, openai_provider: OpenAICompatibleEmbeddingProvider
    ) -> None:
        route = respx.post(OPENAI_URL).mock(
            return_value=httpx.Response(200, json=_openai_response([0.1] * 512))
        )
        req = EmbeddingProviderRequest(
            model="openai/text-embedding-3-small",
            input_texts=["foo"],
            dimensions=512,
        )
        await openai_provider.embed(req)
        assert b'"dimensions":512' in route.calls[0].request.content

    @respx.mock
    @pytest.mark.asyncio
    async def test_auth_error_classifies_as_auth_error(
        self, openai_provider: OpenAICompatibleEmbeddingProvider
    ) -> None:
        respx.post(OPENAI_URL).mock(
            return_value=httpx.Response(401, json={"error": {"message": "Incorrect API key"}})
        )
        req = EmbeddingProviderRequest(
            model="openai/text-embedding-3-small",
            input_texts=["foo"],
        )
        with pytest.raises(AuthError):
            await openai_provider.embed(req)

    @respx.mock
    @pytest.mark.asyncio
    async def test_rate_limit_classifies_correctly(
        self, openai_provider: OpenAICompatibleEmbeddingProvider
    ) -> None:
        respx.post(OPENAI_URL).mock(
            return_value=httpx.Response(429, json={"error": {"message": "too fast"}})
        )
        req = EmbeddingProviderRequest(
            model="openai/text-embedding-3-small",
            input_texts=["foo"],
        )
        with pytest.raises(RateLimitError):
            await openai_provider.embed(req)

    @respx.mock
    @pytest.mark.asyncio
    async def test_upstream_5xx_classifies_as_retryable(
        self, openai_provider: OpenAICompatibleEmbeddingProvider
    ) -> None:
        respx.post(OPENAI_URL).mock(
            return_value=httpx.Response(503, json={"error": {"message": "down"}})
        )
        req = EmbeddingProviderRequest(
            model="openai/text-embedding-3-small",
            input_texts=["foo"],
        )
        with pytest.raises(ProviderError) as exc_info:
            await openai_provider.embed(req)
        assert exc_info.value.retryable is True
        assert exc_info.value.status == 502


class TestOpenAIPricing:
    def test_cost_hcents_per_mtok(self, openai_provider: OpenAICompatibleEmbeddingProvider) -> None:
        # text-embedding-3-small at 2000 hcents/Mtok.
        # 1,000,000 tokens × 2000 / 1_000_000 = 2000 hcents.
        assert openai_provider.cost_hcents(1_000_000, "openai/text-embedding-3-small") == 2_000

    def test_cost_hcents_small_input(
        self, openai_provider: OpenAICompatibleEmbeddingProvider
    ) -> None:
        # 500 tokens × 2000 hcents/Mtok = 1 hcent (integer floor).
        assert openai_provider.cost_hcents(500, "openai/text-embedding-3-small") == 1

    def test_unknown_model_costs_zero(
        self, openai_provider: OpenAICompatibleEmbeddingProvider
    ) -> None:
        assert openai_provider.cost_hcents(1_000_000, "openai/unknown-model") == 0


# --------------------------------------------------------------------------- #
# CohereEmbeddingProvider                                                     #
# --------------------------------------------------------------------------- #


COHERE_URL = "https://api.cohere.com/v2/embed"


@pytest.fixture
def cohere_provider() -> CohereEmbeddingProvider:
    return CohereEmbeddingProvider(
        api_key="test-cohere-key",
        pricing={
            "embed-english-v3.0": Pricing(
                input_hcents_per_mtok=10_000,
                output_hcents_per_mtok=0,
            ),
        },
    )


def _cohere_response(*vectors: list[float], prompt_tokens: int = 8) -> dict:
    return {
        "id": "abc",
        "embeddings": {"float": list(vectors)},
        "texts": ["x"] * len(vectors),
        "meta": {
            "api_version": {"version": "2"},
            "billed_units": {"input_tokens": prompt_tokens},
        },
    }


class TestCohereShape:
    @respx.mock
    @pytest.mark.asyncio
    async def test_uses_texts_field_not_input(
        self, cohere_provider: CohereEmbeddingProvider
    ) -> None:
        route = respx.post(COHERE_URL).mock(
            return_value=httpx.Response(200, json=_cohere_response([0.1, 0.2]))
        )
        req = EmbeddingProviderRequest(
            model="cohere/embed-english-v3.0",
            input_texts=["hello world"],
        )
        result = await cohere_provider.embed(req)
        assert result.vectors == [[0.1, 0.2]]
        assert result.prompt_tokens == 8

        # Verify the adapter sent ``texts`` (not ``input``) and
        # supplied a default ``input_type``.
        body = route.calls[0].request.content
        assert b'"texts"' in body
        assert b'"input"' not in body
        assert b'"input_type"' in body
        # Default for unspecified input_type is search_document.
        assert b'"search_document"' in body

    @respx.mock
    @pytest.mark.asyncio
    async def test_explicit_input_type_overrides_default(
        self, cohere_provider: CohereEmbeddingProvider
    ) -> None:
        route = respx.post(COHERE_URL).mock(
            return_value=httpx.Response(200, json=_cohere_response([0.1]))
        )
        req = EmbeddingProviderRequest(
            model="cohere/embed-english-v3.0",
            input_texts=["q"],
            input_type="search_query",
        )
        await cohere_provider.embed(req)
        body = route.calls[0].request.content
        assert b'"search_query"' in body
        # The default value shouldn't ALSO be present.
        assert b'"search_document"' not in body


# --------------------------------------------------------------------------- #
# VoyageEmbeddingProvider                                                     #
# --------------------------------------------------------------------------- #


VOYAGE_URL = "https://api.voyageai.com/v1/embeddings"


@pytest.fixture
def voyage_provider() -> VoyageEmbeddingProvider:
    return VoyageEmbeddingProvider(
        api_key="test-voyage-key",
        pricing={
            "voyage-3": Pricing(
                input_hcents_per_mtok=6_000,
                output_hcents_per_mtok=0,
            ),
        },
    )


def _voyage_response(*vectors: list[float], total_tokens: int = 5) -> dict:
    return {
        "object": "list",
        "data": [
            {"object": "embedding", "embedding": vec, "index": i} for i, vec in enumerate(vectors)
        ],
        "model": "voyage-3",
        "usage": {"total_tokens": total_tokens},
    }


class TestVoyageShape:
    @respx.mock
    @pytest.mark.asyncio
    async def test_uses_input_field_with_input_type(
        self, voyage_provider: VoyageEmbeddingProvider
    ) -> None:
        route = respx.post(VOYAGE_URL).mock(
            return_value=httpx.Response(200, json=_voyage_response([0.3, 0.4]))
        )
        req = EmbeddingProviderRequest(
            model="voyage/voyage-3",
            input_texts=["search this"],
            input_type="query",
        )
        result = await voyage_provider.embed(req)
        assert result.vectors == [[0.3, 0.4]]
        assert result.prompt_tokens == 5

        body = route.calls[0].request.content
        assert b'"input"' in body
        assert b'"input_type":"query"' in body

    @respx.mock
    @pytest.mark.asyncio
    async def test_input_type_omitted_when_not_supplied(
        self, voyage_provider: VoyageEmbeddingProvider
    ) -> None:
        route = respx.post(VOYAGE_URL).mock(
            return_value=httpx.Response(200, json=_voyage_response([0.1]))
        )
        req = EmbeddingProviderRequest(
            model="voyage/voyage-3",
            input_texts=["plain"],
        )
        await voyage_provider.embed(req)
        body = route.calls[0].request.content
        # When the client didn't specify input_type, the adapter
        # must NOT inject one (lets Voyage's default apply).
        assert b'"input_type"' not in body


# --------------------------------------------------------------------------- #
# Construction errors                                                         #
# --------------------------------------------------------------------------- #


class TestConstruction:
    def test_openai_provider_requires_api_key(self) -> None:
        with pytest.raises(AuthError):
            OpenAICompatibleEmbeddingProvider(
                provider_key="openai",
                base_url="https://api.openai.com/v1",
                api_key="",
                pricing={},
            )

    def test_cohere_provider_requires_api_key(self) -> None:
        with pytest.raises(AuthError):
            CohereEmbeddingProvider(api_key="", pricing={})

    def test_voyage_provider_requires_api_key(self) -> None:
        with pytest.raises(AuthError):
            VoyageEmbeddingProvider(api_key="", pricing={})
