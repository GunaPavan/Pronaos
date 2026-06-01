"""Unit tests for the rerank provider adapters (Phase 32).

Two HTTP-backed adapters (Cohere, Voyage); each tested against a
mocked upstream via ``respx``. Tests assert:

- request shape matches the upstream's expected schema (Cohere's
  ``top_n``, Voyage's ``top_k``)
- response parsing extracts scored items in upstream order
- error classes (auth, rate limit, upstream 5xx) map to the shared
  error hierarchy
- pricing math differs by shape (Cohere = per-call; Voyage = per-token)
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
from pronaos.providers.openai_compat import Pricing
from pronaos.providers.rerank import (
    CohereRerankProvider,
    RerankProviderRequest,
    VoyageRerankProvider,
)

# --------------------------------------------------------------------------- #
# Cohere                                                                      #
# --------------------------------------------------------------------------- #


COHERE_URL = "https://api.cohere.com/v2/rerank"


@pytest.fixture
def cohere_provider() -> CohereRerankProvider:
    return CohereRerankProvider(
        api_key="test-cohere-key",
        # Per-call pricing: 20 hcents per call (Cohere $2/1000 search units).
        # Pricing.input_hcents_per_mtok is reused as "per-call hcents" for
        # rerank — see catalog.py.
        pricing={
            "rerank-english-v3.0": Pricing(input_hcents_per_mtok=20, output_hcents_per_mtok=0),
        },
    )


def _cohere_response(
    *items: tuple[int, float, str],
    search_units: int = 1,
) -> dict:
    """Build a Cohere /v2/rerank response.

    Each item is ``(index, score, document_text)``.
    """
    return {
        "id": "rerank-abc",
        "results": [
            {
                "index": i,
                "relevance_score": s,
                "document": {"text": t},
            }
            for (i, s, t) in items
        ],
        "meta": {
            "api_version": {"version": "2"},
            "billed_units": {"search_units": search_units},
        },
    }


class TestCohereShape:
    @respx.mock
    @pytest.mark.asyncio
    async def test_uses_top_n_and_returns_scored_items(
        self, cohere_provider: CohereRerankProvider
    ) -> None:
        route = respx.post(COHERE_URL).mock(
            return_value=httpx.Response(
                200,
                json=_cohere_response(
                    (2, 0.99, "Washington, D.C. is the capital."),
                    (0, 0.07, "Carson City is the capital of Nevada."),
                ),
            )
        )
        req = RerankProviderRequest(
            model="cohere/rerank-english-v3.0",
            query="What's the capital of the US?",
            documents=["Carson City…", "Tokyo is…", "Washington, D.C…"],
            top_n=2,
        )
        result = await cohere_provider.rerank(req)
        assert len(result.results) == 2
        assert result.results[0].index == 2
        assert result.results[0].relevance_score == pytest.approx(0.99)
        assert result.results[0].document == "Washington, D.C. is the capital."
        assert result.results[1].index == 0
        # search_units becomes prompt_tokens for the usage record.
        assert result.prompt_tokens == 1

        body = route.calls[0].request.content
        assert b'"top_n":2' in body
        # Cohere shape: ``top_n``, NOT ``top_k``.
        assert b'"top_k"' not in body
        # Model prefix stripped.
        assert b'"model":"rerank-english-v3.0"' in body
        # Query + documents present.
        assert b'"query"' in body
        assert b'"documents"' in body

    @respx.mock
    @pytest.mark.asyncio
    async def test_omits_top_n_when_none(self, cohere_provider: CohereRerankProvider) -> None:
        route = respx.post(COHERE_URL).mock(
            return_value=httpx.Response(200, json=_cohere_response((0, 0.5, "doc")))
        )
        req = RerankProviderRequest(
            model="cohere/rerank-english-v3.0",
            query="q",
            documents=["doc"],
            top_n=None,
        )
        await cohere_provider.rerank(req)
        body = route.calls[0].request.content
        # When top_n is omitted on the public side, it must NOT be sent.
        assert b'"top_n"' not in body

    @respx.mock
    @pytest.mark.asyncio
    async def test_return_documents_false_handled(
        self, cohere_provider: CohereRerankProvider
    ) -> None:
        # When client sets return_documents=False, upstream returns
        # entries without a "document" field; adapter must still parse.
        payload = {
            "results": [
                {"index": 0, "relevance_score": 0.42},
                {"index": 1, "relevance_score": 0.20},
            ],
            "meta": {"billed_units": {"search_units": 1}},
        }
        respx.post(COHERE_URL).mock(return_value=httpx.Response(200, json=payload))
        req = RerankProviderRequest(
            model="cohere/rerank-english-v3.0",
            query="q",
            documents=["a", "b"],
            return_documents=False,
        )
        result = await cohere_provider.rerank(req)
        assert result.results[0].document is None
        assert result.results[0].index == 0

    @respx.mock
    @pytest.mark.asyncio
    async def test_auth_error(self, cohere_provider: CohereRerankProvider) -> None:
        respx.post(COHERE_URL).mock(
            return_value=httpx.Response(401, json={"error": {"message": "bad"}})
        )
        req = RerankProviderRequest(model="cohere/rerank-english-v3.0", query="q", documents=["d"])
        with pytest.raises(AuthError):
            await cohere_provider.rerank(req)

    @respx.mock
    @pytest.mark.asyncio
    async def test_rate_limit(self, cohere_provider: CohereRerankProvider) -> None:
        respx.post(COHERE_URL).mock(
            return_value=httpx.Response(429, json={"error": {"message": "slow"}})
        )
        req = RerankProviderRequest(model="cohere/rerank-english-v3.0", query="q", documents=["d"])
        with pytest.raises(RateLimitError):
            await cohere_provider.rerank(req)

    @respx.mock
    @pytest.mark.asyncio
    async def test_upstream_5xx(self, cohere_provider: CohereRerankProvider) -> None:
        respx.post(COHERE_URL).mock(
            return_value=httpx.Response(503, json={"error": {"message": "down"}})
        )
        req = RerankProviderRequest(model="cohere/rerank-english-v3.0", query="q", documents=["d"])
        with pytest.raises(ProviderError) as exc:
            await cohere_provider.rerank(req)
        assert exc.value.retryable is True


class TestCoherePricing:
    def test_per_call_hcents(self, cohere_provider: CohereRerankProvider) -> None:
        # search_units=1 (one call) × 20 hcents/call = 20 hcents.
        assert cohere_provider.cost_hcents(1, "cohere/rerank-english-v3.0") == 20

    def test_unknown_model_returns_zero(self, cohere_provider: CohereRerankProvider) -> None:
        assert cohere_provider.cost_hcents(1, "cohere/unknown") == 0

    def test_zero_search_units_defaults_to_one(self, cohere_provider: CohereRerankProvider) -> None:
        # If the upstream didn't report search_units (some legacy paths),
        # we still charge for the call: max(1, 0) × 20 = 20.
        assert cohere_provider.cost_hcents(0, "cohere/rerank-english-v3.0") == 20


# --------------------------------------------------------------------------- #
# Voyage                                                                      #
# --------------------------------------------------------------------------- #


VOYAGE_URL = "https://api.voyageai.com/v1/rerank"


@pytest.fixture
def voyage_provider() -> VoyageRerankProvider:
    return VoyageRerankProvider(
        api_key="test-voyage-key",
        pricing={
            "rerank-2": Pricing(input_hcents_per_mtok=5_000, output_hcents_per_mtok=0),
        },
    )


def _voyage_response(*items: tuple[int, float, str], total_tokens: int = 38) -> dict:
    return {
        "object": "list",
        "data": [
            {
                "index": i,
                "relevance_score": s,
                "document": t,
            }
            for (i, s, t) in items
        ],
        "model": "rerank-2",
        "usage": {"total_tokens": total_tokens},
    }


class TestVoyageShape:
    @respx.mock
    @pytest.mark.asyncio
    async def test_translates_top_n_to_top_k(self, voyage_provider: VoyageRerankProvider) -> None:
        route = respx.post(VOYAGE_URL).mock(
            return_value=httpx.Response(
                200,
                json=_voyage_response(
                    (2, 0.95, "Washington, D.C…"),
                    (0, 0.05, "Carson City…"),
                ),
            )
        )
        req = RerankProviderRequest(
            model="voyage/rerank-2",
            query="capital?",
            documents=["a", "b", "c"],
            top_n=2,
        )
        result = await voyage_provider.rerank(req)
        assert len(result.results) == 2
        assert result.results[0].index == 2
        assert result.results[0].relevance_score == pytest.approx(0.95)
        assert result.prompt_tokens == 38

        body = route.calls[0].request.content
        # Voyage spelling is top_k. The adapter must translate from
        # the public top_n.
        assert b'"top_k":2' in body
        assert b'"top_n"' not in body
        # Model prefix stripped.
        assert b'"model":"rerank-2"' in body

    @respx.mock
    @pytest.mark.asyncio
    async def test_response_parsing_keeps_upstream_order(
        self, voyage_provider: VoyageRerankProvider
    ) -> None:
        # Voyage already returns sorted by score; adapter trusts that.
        payload = {
            "object": "list",
            "data": [
                {"index": 5, "relevance_score": 0.9, "document": "high"},
                {"index": 0, "relevance_score": 0.5, "document": "mid"},
                {"index": 2, "relevance_score": 0.1, "document": "low"},
            ],
            "model": "rerank-2",
            "usage": {"total_tokens": 50},
        }
        respx.post(VOYAGE_URL).mock(return_value=httpx.Response(200, json=payload))
        req = RerankProviderRequest(model="voyage/rerank-2", query="q", documents=["x"] * 6)
        result = await voyage_provider.rerank(req)
        assert [r.index for r in result.results] == [5, 0, 2]
        assert [r.relevance_score for r in result.results] == [
            pytest.approx(0.9),
            pytest.approx(0.5),
            pytest.approx(0.1),
        ]

    @respx.mock
    @pytest.mark.asyncio
    async def test_auth_error(self, voyage_provider: VoyageRerankProvider) -> None:
        respx.post(VOYAGE_URL).mock(return_value=httpx.Response(403, json={"error": "no"}))
        req = RerankProviderRequest(model="voyage/rerank-2", query="q", documents=["d"])
        with pytest.raises(AuthError):
            await voyage_provider.rerank(req)


class TestVoyagePricing:
    def test_per_token_pricing(self, voyage_provider: VoyageRerankProvider) -> None:
        # 1_000_000 tokens × 5000 hcents/Mtok / 1_000_000 = 5000 hcents.
        assert voyage_provider.cost_hcents(1_000_000, "voyage/rerank-2") == 5_000

    def test_small_input_floors_to_zero(self, voyage_provider: VoyageRerankProvider) -> None:
        # 100 tokens × 5000 / 1_000_000 = 0.5 → integer floor 0.
        assert voyage_provider.cost_hcents(100, "voyage/rerank-2") == 0

    def test_unknown_model_returns_zero(self, voyage_provider: VoyageRerankProvider) -> None:
        assert voyage_provider.cost_hcents(1_000_000, "voyage/unknown") == 0


# --------------------------------------------------------------------------- #
# Construction                                                                #
# --------------------------------------------------------------------------- #


class TestConstruction:
    def test_cohere_requires_api_key(self) -> None:
        with pytest.raises(AuthError):
            CohereRerankProvider(api_key="", pricing={})

    def test_voyage_requires_api_key(self) -> None:
        with pytest.raises(AuthError):
            VoyageRerankProvider(api_key="", pricing={})
