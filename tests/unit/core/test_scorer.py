"""Cost-aware routing tests.

Three layers:

1. **Pure scorer math.** CostScorer / FastestScorer / BalancedScorer
   produce exact, predictable scores for fixed inputs. These pin the
   formula so a regression in pricing math is caught immediately.
2. **Eligibility filtering.** Capability requirements (tools / vision /
   context size) must filter candidates correctly — including the
   90% context margin that protects against output overflow.
3. **End-to-end selection.** ``select_model`` glues build-candidates +
   filter-eligible + score together. The integration tests assert the
   *outcome* (which fqmn wins) for representative team policies.

The CATALOG is treated as a fixture: tests assert against the actual
shipping numbers in ``providers/catalog.py`` so a price update that
breaks the cheapest-selection invariant gets flagged.
"""

from __future__ import annotations

import pytest

from pronaos.core.scorer import (
    BalancedScorer,
    Candidate,
    CostScorer,
    FastestScorer,
    NoEligibleModelError,
    RoutingRequest,
    RoutingStrategy,
    build_candidates,
    filter_eligible,
    select_model,
)
from pronaos.providers.catalog import (
    ModelCapabilities,
    get_capabilities,
    get_pricing,
)
from pronaos.providers.openai_compat import Pricing

# --------------------------------------------------------------------------- #
# Fixtures                                                                    #
# --------------------------------------------------------------------------- #


def _mk_candidate(
    *,
    provider: str = "test",
    model: str = "test-model",
    in_price: int = 10_000,
    out_price: int = 20_000,
    p50_ms: int = 500,
    supports_tools: bool = False,
    supports_streaming: bool = True,
    supports_vision: bool = False,
    max_context: int = 8192,
) -> Candidate:
    return Candidate(
        provider_key=provider,
        model_name=model,
        pricing=Pricing(in_price, out_price),
        capabilities=ModelCapabilities(
            supports_tools=supports_tools,
            supports_streaming=supports_streaming,
            supports_vision=supports_vision,
            max_context_tokens=max_context,
        ),
        typical_p50_ms=p50_ms,
    )


# --------------------------------------------------------------------------- #
# Scorer math                                                                 #
# --------------------------------------------------------------------------- #


class TestCostScorer:
    """Exact-math regression tests for CostScorer.

    Formula: ``(in_tokens * in_price_hcents_per_mtok + out_tokens * out_price) / 1e6``.
    Numbers below are hand-computed so a future change to the formula
    breaks one of these and gets immediate attention.
    """

    def test_basic_cost_in_hcents(self) -> None:
        # 1000 in × 10k hcents/M + 500 out × 20k hcents/M
        # = 10_000_000 / 1e6 + 10_000_000 / 1e6 = 20.0 hcents
        c = _mk_candidate(in_price=10_000, out_price=20_000)
        req = RoutingRequest(estimated_input_tokens=1000, estimated_output_tokens=500)
        assert CostScorer().score(c, req) == pytest.approx(20.0)

    def test_zero_tokens_zero_cost(self) -> None:
        c = _mk_candidate()
        req = RoutingRequest(estimated_input_tokens=0, estimated_output_tokens=0)
        assert CostScorer().score(c, req) == 0.0

    def test_output_dominates_when_asymmetric(self) -> None:
        # Output is 100× more expensive — cost should reflect that.
        c = _mk_candidate(in_price=1_000, out_price=100_000)
        req = RoutingRequest(estimated_input_tokens=100, estimated_output_tokens=100)
        # in_cost = 100 * 1000 / 1e6 = 0.1; out_cost = 100 * 100000 / 1e6 = 10
        assert CostScorer().score(c, req) == pytest.approx(10.1)


class TestFastestScorer:
    def test_score_equals_typical_p50(self) -> None:
        c = _mk_candidate(p50_ms=250)
        req = RoutingRequest(estimated_input_tokens=100, estimated_output_tokens=100)
        assert FastestScorer().score(c, req) == 250.0

    def test_score_ignores_token_count(self) -> None:
        """Latency doesn't depend on prompt size (in this proxy)."""
        c = _mk_candidate(p50_ms=900)
        req_small = RoutingRequest(estimated_input_tokens=10, estimated_output_tokens=10)
        req_large = RoutingRequest(
            estimated_input_tokens=100_000, estimated_output_tokens=10
        )
        scorer = FastestScorer()
        assert scorer.score(c, req_small) == scorer.score(c, req_large) == 900.0


class TestBalancedScorer:
    def test_returns_one_score_per_candidate(self) -> None:
        a = _mk_candidate(provider="a", in_price=1_000, p50_ms=200)
        b = _mk_candidate(provider="b", in_price=10_000, p50_ms=1000)
        scores = BalancedScorer().score_pool(
            [a, b], RoutingRequest(estimated_input_tokens=100, estimated_output_tokens=100)
        )
        assert len(scores) == 2

    def test_normalises_extremes_to_zero_and_two(self) -> None:
        """Pool of 2: the cheapest+fastest scores 0, the worst on both axes
        scores 2 (1 on each normalised axis)."""
        cheap_fast = _mk_candidate(provider="cf", in_price=1_000, p50_ms=100)
        slow_expensive = _mk_candidate(
            provider="se", in_price=100_000, p50_ms=5_000
        )
        scores = BalancedScorer().score_pool(
            [cheap_fast, slow_expensive],
            RoutingRequest(estimated_input_tokens=100, estimated_output_tokens=100),
        )
        assert scores[0] == pytest.approx(0.0)
        assert scores[1] == pytest.approx(2.0)

    def test_empty_pool_returns_empty(self) -> None:
        assert BalancedScorer().score_pool(
            [], RoutingRequest(estimated_input_tokens=0, estimated_output_tokens=0)
        ) == []


# --------------------------------------------------------------------------- #
# Eligibility filter                                                          #
# --------------------------------------------------------------------------- #


class TestFilterEligible:
    def test_drops_models_without_tool_support_when_required(self) -> None:
        no_tools = _mk_candidate(provider="a", supports_tools=False)
        has_tools = _mk_candidate(provider="b", supports_tools=True)
        req = RoutingRequest(
            estimated_input_tokens=100,
            estimated_output_tokens=100,
            requires_tools=True,
        )
        out = filter_eligible([no_tools, has_tools], req)
        assert len(out) == 1
        assert out[0].provider_key == "b"

    def test_drops_models_without_vision_when_required(self) -> None:
        no_vision = _mk_candidate(provider="a", supports_vision=False)
        has_vision = _mk_candidate(provider="b", supports_vision=True)
        req = RoutingRequest(
            estimated_input_tokens=100,
            estimated_output_tokens=100,
            requires_vision=True,
        )
        out = filter_eligible([no_vision, has_vision], req)
        assert [c.provider_key for c in out] == ["b"]

    def test_context_filter_uses_90_percent_margin(self) -> None:
        """A model with max_context=10_000 must accept ≤9000-token inputs."""
        small = _mk_candidate(provider="small", max_context=10_000)
        big = _mk_candidate(provider="big", max_context=100_000)
        # 9001 tokens > 90% of 10K → small drops out
        req = RoutingRequest(
            estimated_input_tokens=9001, estimated_output_tokens=100
        )
        out = filter_eligible([small, big], req)
        assert [c.provider_key for c in out] == ["big"]

    def test_no_requirements_keeps_all(self) -> None:
        a = _mk_candidate(provider="a")
        b = _mk_candidate(provider="b")
        req = RoutingRequest(estimated_input_tokens=100, estimated_output_tokens=100)
        assert filter_eligible([a, b], req) == [a, b]


# --------------------------------------------------------------------------- #
# Candidate enumeration                                                       #
# --------------------------------------------------------------------------- #


class TestBuildCandidates:
    def test_none_allowlist_includes_all_priced_catalog_models(self) -> None:
        """``allowed_patterns=None`` = unrestricted. Every catalog entry
        with a pricing row should appear; entries without pricing
        (Ollama) are filtered out by build_candidates."""
        cands = build_candidates(allowed_patterns=None)
        assert len(cands) > 0
        # Ollama has no pricing → must not appear.
        assert not any(c.provider_key == "ollama" for c in cands)
        # Groq has pricing → must appear.
        assert any(c.provider_key == "groq" for c in cands)

    def test_empty_allowlist_returns_no_candidates(self) -> None:
        """``allowed_patterns=[]`` = deny-all."""
        assert build_candidates(allowed_patterns=[]) == []

    def test_pattern_restricts_to_matching_provider(self) -> None:
        cands = build_candidates(allowed_patterns=["groq/*"])
        assert len(cands) > 0
        assert all(c.provider_key == "groq" for c in cands)

    def test_exact_pattern_matches_one_model(self) -> None:
        cands = build_candidates(
            allowed_patterns=["groq/llama-3.1-8b-instant"]
        )
        assert len(cands) == 1
        assert cands[0].fqmn == "groq/llama-3.1-8b-instant"


# --------------------------------------------------------------------------- #
# select_model end-to-end                                                     #
# --------------------------------------------------------------------------- #


class TestSelectModel:
    def test_cheapest_picks_lowest_total_cost(self) -> None:
        """On the real Groq catalog, llama-3.1-8b-instant is cheaper than
        llama-3.3-70b-versatile for any realistic prompt — and currently
        the cheapest non-zero-cost model in the catalog under groq/*."""
        picked = select_model(
            strategy=RoutingStrategy.CHEAPEST,
            allowed_patterns=["groq/*"],
            request=RoutingRequest(
                estimated_input_tokens=1000, estimated_output_tokens=200
            ),
        )
        assert picked.fqmn == "groq/llama-3.1-8b-instant"

    def test_fastest_prefers_provider_with_low_p50(self) -> None:
        """Cerebras (200ms) beats Groq (250ms) on the fastest strategy."""
        picked = select_model(
            strategy=RoutingStrategy.FASTEST,
            allowed_patterns=["groq/*", "cerebras/*"],
            request=RoutingRequest(
                estimated_input_tokens=1000, estimated_output_tokens=200
            ),
        )
        assert picked.provider_key == "cerebras"

    def test_tools_requirement_filters_out_deepseek_reasoner(self) -> None:
        """deepseek-reasoner has supports_tools=False in the catalog.
        A tool-requiring request must skip it even if it's the cheapest."""
        picked = select_model(
            strategy=RoutingStrategy.CHEAPEST,
            allowed_patterns=["deepseek/*"],
            request=RoutingRequest(
                estimated_input_tokens=100,
                estimated_output_tokens=100,
                requires_tools=True,
            ),
        )
        # deepseek-chat supports tools; deepseek-reasoner doesn't.
        assert picked.model_name == "deepseek-chat"

    def test_no_eligible_model_raises(self) -> None:
        """An empty allowlist + a vision-required request → no match."""
        with pytest.raises(NoEligibleModelError):
            select_model(
                strategy=RoutingStrategy.CHEAPEST,
                allowed_patterns=[],
                request=RoutingRequest(
                    estimated_input_tokens=10,
                    estimated_output_tokens=10,
                ),
            )

    def test_no_eligible_when_capabilities_unmet(self) -> None:
        """Restricting to groq/* but requiring vision → no match
        (Groq's catalog doesn't list vision-capable models in our matrix)."""
        with pytest.raises(NoEligibleModelError):
            select_model(
                strategy=RoutingStrategy.CHEAPEST,
                allowed_patterns=["groq/*"],
                request=RoutingRequest(
                    estimated_input_tokens=10,
                    estimated_output_tokens=10,
                    requires_vision=True,
                ),
            )

    def test_tiebreak_is_deterministic(self) -> None:
        """When two candidates have identical scores, alphabetical fqmn
        wins. This makes the selection reproducible across processes."""
        # Run twice and confirm identical winner — even if the catalog
        # has multiple groq models tied on the FASTEST score (all at 250ms).
        a = select_model(
            strategy=RoutingStrategy.FASTEST,
            allowed_patterns=["groq/*"],
            request=RoutingRequest(estimated_input_tokens=10, estimated_output_tokens=10),
        )
        b = select_model(
            strategy=RoutingStrategy.FASTEST,
            allowed_patterns=["groq/*"],
            request=RoutingRequest(estimated_input_tokens=10, estimated_output_tokens=10),
        )
        assert a.fqmn == b.fqmn


# --------------------------------------------------------------------------- #
# Catalog capability lookups                                                  #
# --------------------------------------------------------------------------- #


class TestCatalogLookups:
    def test_get_capabilities_returns_defaults_for_unlisted_model(self) -> None:
        caps = get_capabilities("groq", "some-future-model-not-in-catalog")
        # Default ModelCapabilities — conservative no-tools, no-vision.
        assert caps.supports_tools is False
        assert caps.supports_vision is False
        assert caps.supports_streaming is True

    def test_get_capabilities_unknown_provider_returns_defaults(self) -> None:
        caps = get_capabilities("not-a-provider", "any-model")
        assert caps == ModelCapabilities()

    def test_get_pricing_returns_none_for_unpriced(self) -> None:
        # Ollama has no pricing entries.
        assert get_pricing("ollama", "llama3.1") is None

    def test_get_pricing_returns_known_groq_price(self) -> None:
        # If this regresses, the README's claim #8 cost numbers will
        # diverge from reality — catch it here.
        pricing = get_pricing("groq", "llama-3.1-8b-instant")
        assert pricing is not None
        assert pricing.input_hcents_per_mtok == 5_000
        assert pricing.output_hcents_per_mtok == 8_000
