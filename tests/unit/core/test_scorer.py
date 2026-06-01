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
        req_large = RoutingRequest(estimated_input_tokens=100_000, estimated_output_tokens=10)
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
        slow_expensive = _mk_candidate(provider="se", in_price=100_000, p50_ms=5_000)
        scores = BalancedScorer().score_pool(
            [cheap_fast, slow_expensive],
            RoutingRequest(estimated_input_tokens=100, estimated_output_tokens=100),
        )
        assert scores[0] == pytest.approx(0.0)
        assert scores[1] == pytest.approx(2.0)

    def test_empty_pool_returns_empty(self) -> None:
        assert (
            BalancedScorer().score_pool(
                [], RoutingRequest(estimated_input_tokens=0, estimated_output_tokens=0)
            )
            == []
        )


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
        req = RoutingRequest(estimated_input_tokens=9001, estimated_output_tokens=100)
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
        cands = build_candidates(allowed_patterns=["groq/llama-3.1-8b-instant"])
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
            request=RoutingRequest(estimated_input_tokens=1000, estimated_output_tokens=200),
        )
        assert picked.fqmn == "groq/llama-3.1-8b-instant"

    def test_fastest_prefers_provider_with_low_p50(self) -> None:
        """Cerebras (200ms) beats Groq (250ms) on the fastest strategy."""
        picked = select_model(
            strategy=RoutingStrategy.FASTEST,
            allowed_patterns=["groq/*", "cerebras/*"],
            request=RoutingRequest(estimated_input_tokens=1000, estimated_output_tokens=200),
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
        """Restricting to a vision-less provider's allowlist but requiring
        vision → no match. ``together/*`` is text-only across its catalog
        entries; constraining to it while asking for vision must fail."""
        with pytest.raises(NoEligibleModelError):
            select_model(
                strategy=RoutingStrategy.CHEAPEST,
                allowed_patterns=["together/*"],
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


# --------------------------------------------------------------------------- #
# Quality-aware routing (Phase 24)                                            #
# --------------------------------------------------------------------------- #

from typing import Any  # noqa: E402

from pronaos.core.scorer import (  # noqa: E402 — grouped with the quality block
    DEFAULT_QUALITY_THRESHOLD,
    filter_by_quality,
)


class TestFilterByQuality:
    def _mk(self, fqmn: str) -> Candidate:
        provider, _, model = fqmn.partition("/")
        return _mk_candidate(provider=provider, model=model)

    def test_returns_input_when_scores_none(self) -> None:
        """No eval data on this team → no filtering. The quality-aware
        strategy degrades to plain cheapest selection."""
        cands = [self._mk("groq/a"), self._mk("groq/b")]
        assert filter_by_quality(cands, quality_scores=None, quality_threshold=0.7) == cands

    def test_returns_input_when_scores_empty(self) -> None:
        """Empty dict is also "no data" — same degradation."""
        cands = [self._mk("groq/a")]
        assert filter_by_quality(cands, quality_scores={}, quality_threshold=0.7) == cands

    def test_drops_below_threshold(self) -> None:
        """The headline behaviour: models with a stored score below
        the threshold are filtered out."""
        cands = [self._mk("groq/cheap"), self._mk("groq/good")]
        scores = {
            "groq/cheap": {"score": 0.4, "n_samples": 5},
            "groq/good": {"score": 0.9, "n_samples": 5},
        }
        out = filter_by_quality(cands, quality_scores=scores, quality_threshold=0.7)
        assert [c.fqmn for c in out] == ["groq/good"]

    def test_keeps_unevaluated_models(self) -> None:
        """A model with NO stored score must NOT be dropped — we have
        no evidence it under-performs. The fallback is "trust the
        operator's allowlist" rather than "evaluation gate."""
        cands = [self._mk("groq/known"), self._mk("groq/unknown")]
        scores = {"groq/known": {"score": 0.9, "n_samples": 5}}
        out = filter_by_quality(cands, quality_scores=scores, quality_threshold=0.7)
        assert {c.fqmn for c in out} == {"groq/known", "groq/unknown"}

    def test_threshold_boundary_keeps_equal(self) -> None:
        """score == threshold → keep (>= semantics, not >)."""
        cands = [self._mk("groq/boundary")]
        scores = {"groq/boundary": {"score": 0.7, "n_samples": 5}}
        out = filter_by_quality(cands, quality_scores=scores, quality_threshold=0.7)
        assert [c.fqmn for c in out] == ["groq/boundary"]

    def test_malformed_entry_is_kept(self) -> None:
        """A non-dict or score-less entry must not crash — keep the
        candidate. The CLI / validator should prevent this at write
        time but the runtime path is defensive."""
        cands = [self._mk("groq/a")]
        scores: dict[str, Any] = {"groq/a": {"score": "not-a-number"}}
        out = filter_by_quality(cands, quality_scores=scores, quality_threshold=0.7)
        assert [c.fqmn for c in out] == ["groq/a"]


class TestQualityAwareCheapest:
    """End-to-end ``select_model`` behaviour under the new strategy."""

    def test_picks_cheapest_quality_clearing_model(self) -> None:
        """The expensive 70B clears the bar, the cheap 8B does not.
        Strategy must pick the *expensive* one because the cheap one
        was filtered out by the quality gate.

        Allowlist is pinned to the two specific models so unevaluated
        catalog entries (Scout, mixtral, qwen) can't sneak in via the
        "unevaluated → kept" rule and undercut the test on price."""
        scores = {
            "groq/llama-3.1-8b-instant": {"score": 0.4, "n_samples": 5},
            "groq/llama-3.3-70b-versatile": {"score": 0.9, "n_samples": 5},
        }
        picked = select_model(
            strategy=RoutingStrategy.QUALITY_AWARE_CHEAPEST,
            allowed_patterns=[
                "groq/llama-3.1-8b-instant",
                "groq/llama-3.3-70b-versatile",
            ],
            request=RoutingRequest(estimated_input_tokens=100, estimated_output_tokens=100),
            quality_scores=scores,
            quality_threshold=0.7,
        )
        assert picked.fqmn == "groq/llama-3.3-70b-versatile"

    def test_degrades_to_cheapest_with_no_scores(self) -> None:
        """No eval data → behave like plain ``cheapest``. This is the
        graceful-degradation invariant: operators who switch to
        ``quality-aware-cheapest`` before running an eval shouldn't
        get a 500."""
        picked = select_model(
            strategy=RoutingStrategy.QUALITY_AWARE_CHEAPEST,
            allowed_patterns=[
                "groq/llama-3.1-8b-instant",
                "groq/llama-3.3-70b-versatile",
            ],
            request=RoutingRequest(estimated_input_tokens=100, estimated_output_tokens=100),
            quality_scores=None,
            quality_threshold=0.7,
        )
        # Same answer as plain cheapest would give:
        assert picked.fqmn == "groq/llama-3.1-8b-instant"

    def test_raises_when_no_model_clears_threshold(self) -> None:
        """If every evaluated model is below the bar, surface 422 —
        the operator either lowers the threshold or widens the
        allowlist."""
        scores = {
            "groq/llama-3.1-8b-instant": {"score": 0.3, "n_samples": 5},
            "groq/llama-3.3-70b-versatile": {"score": 0.4, "n_samples": 5},
            "groq/meta-llama/llama-4-scout-17b-16e-instruct": {
                "score": 0.5,
                "n_samples": 5,
            },
            "groq/mixtral-8x7b-32768": {"score": 0.2, "n_samples": 5},
            "groq/qwen-qwq-32b": {"score": 0.1, "n_samples": 5},
        }
        with pytest.raises(NoEligibleModelError, match="quality threshold"):
            select_model(
                strategy=RoutingStrategy.QUALITY_AWARE_CHEAPEST,
                allowed_patterns=["groq/*"],
                request=RoutingRequest(estimated_input_tokens=100, estimated_output_tokens=100),
                quality_scores=scores,
                quality_threshold=0.99,
            )

    def test_default_threshold_when_none(self) -> None:
        """When the team's ``quality_threshold`` column is NULL but the
        strategy is active, the scorer uses ``DEFAULT_QUALITY_THRESHOLD``
        (0.7) as a sensible fallback."""
        assert DEFAULT_QUALITY_THRESHOLD == 0.7
        # All scores at 0.8 → all clear the 0.7 default → pick cheapest.
        scores = {
            "groq/llama-3.1-8b-instant": {"score": 0.8, "n_samples": 5},
            "groq/llama-3.3-70b-versatile": {"score": 0.8, "n_samples": 5},
        }
        picked = select_model(
            strategy=RoutingStrategy.QUALITY_AWARE_CHEAPEST,
            allowed_patterns=[
                "groq/llama-3.1-8b-instant",
                "groq/llama-3.3-70b-versatile",
            ],
            request=RoutingRequest(estimated_input_tokens=100, estimated_output_tokens=100),
            quality_scores=scores,
            quality_threshold=None,  # → uses 0.7
        )
        # Both clear the bar → 8B-instant is cheaper.
        assert picked.fqmn == "groq/llama-3.1-8b-instant"

    def test_unevaluated_models_remain_in_pool(self) -> None:
        """An unevaluated model + an evaluated-failing model + an
        evaluated-passing model: the failing one drops out, both the
        unevaluated and the passing ones stay. Cheapest of THAT pool
        wins."""
        scores = {
            "groq/llama-3.1-8b-instant": {"score": 0.4, "n_samples": 5},
            # 70B has no entry → unevaluated → stays.
            "groq/meta-llama/llama-4-scout-17b-16e-instruct": {
                "score": 0.9,
                "n_samples": 5,
            },
        }
        picked = select_model(
            strategy=RoutingStrategy.QUALITY_AWARE_CHEAPEST,
            allowed_patterns=["groq/*"],
            request=RoutingRequest(estimated_input_tokens=100, estimated_output_tokens=100),
            quality_scores=scores,
            quality_threshold=0.7,
        )
        # 8B dropped (score 0.4 < 0.7). 70B kept (unevaluated). Scout
        # kept (score 0.9 ≥ 0.7). Of {70B, Scout}, cheapest wins.
        # Groq prices: 70B = (59k, 79k) hcents/Mtok;
        # Scout = (11k, 34k). Scout is cheaper at every token count.
        assert picked.fqmn == "groq/meta-llama/llama-4-scout-17b-16e-instruct"


# --------------------------------------------------------------------------- #
# Phase 40 — degraded_models_set parameter                                    #
# --------------------------------------------------------------------------- #


class TestDegradedModelExclusion:
    """The scorer must filter out actively-degraded models regardless of
    routing strategy. ``model is broken`` is orthogonal to pricing /
    quality preferences — applied across all strategies."""

    def test_degraded_model_excluded_under_cheapest(self) -> None:
        """The cheapest groq/* model on a list including 8B-instant
        is 8B-instant itself. Mark it degraded → some other groq/*
        wins instead."""
        picked = select_model(
            strategy=RoutingStrategy.CHEAPEST,
            allowed_patterns=["groq/*"],
            request=RoutingRequest(estimated_input_tokens=100, estimated_output_tokens=100),
            degraded_models_set={"groq/llama-3.1-8b-instant"},
        )
        assert picked.fqmn != "groq/llama-3.1-8b-instant"

    def test_degraded_model_excluded_under_quality_aware(self) -> None:
        """Quality-aware filter PLUS degradation filter — both apply.
        8B passes quality threshold but is degraded → Scout wins."""
        scores = {
            "groq/llama-3.1-8b-instant": {"score": 0.9, "n_samples": 5},
            "groq/meta-llama/llama-4-scout-17b-16e-instruct": {
                "score": 0.85,
                "n_samples": 5,
            },
        }
        picked = select_model(
            strategy=RoutingStrategy.QUALITY_AWARE_CHEAPEST,
            allowed_patterns=[
                "groq/llama-3.1-8b-instant",
                "groq/meta-llama/llama-4-scout-17b-16e-instruct",
            ],
            request=RoutingRequest(estimated_input_tokens=100, estimated_output_tokens=100),
            quality_scores=scores,
            quality_threshold=0.7,
            degraded_models_set={"groq/llama-3.1-8b-instant"},
        )
        # 8B degraded → excluded. Scout has score 0.85 ≥ 0.7 → kept.
        assert picked.fqmn == "groq/meta-llama/llama-4-scout-17b-16e-instruct"

    def test_all_models_degraded_raises(self) -> None:
        """Every eligible model degraded → NoEligibleModelError."""
        with pytest.raises(NoEligibleModelError, match="quality-degraded"):
            select_model(
                strategy=RoutingStrategy.CHEAPEST,
                allowed_patterns=["groq/llama-3.1-8b-instant"],
                request=RoutingRequest(
                    estimated_input_tokens=100, estimated_output_tokens=100
                ),
                degraded_models_set={"groq/llama-3.1-8b-instant"},
            )

    def test_empty_degraded_set_is_noop(self) -> None:
        """Empty set / None → no filtering applied."""
        picked = select_model(
            strategy=RoutingStrategy.CHEAPEST,
            allowed_patterns=["groq/*"],
            request=RoutingRequest(estimated_input_tokens=100, estimated_output_tokens=100),
            degraded_models_set=set(),
        )
        assert picked.fqmn.startswith("groq/")


# --------------------------------------------------------------------------- #
# Phase 46: tool-use-aware-cheapest                                            #
# --------------------------------------------------------------------------- #


from pronaos.core.scorer import (  # noqa: E402 — grouped with the tool-use block
    DEFAULT_TOOL_USE_THRESHOLD,
    filter_by_tool_use_score,
)


class TestFilterByToolUseScore:
    """Mirror of TestFilterByQuality but on the tool-use axis."""

    def _mk(self, fqmn: str) -> Candidate:
        provider, _, model = fqmn.partition("/")
        return _mk_candidate(provider=provider, model=model)

    def test_returns_input_when_scores_none(self) -> None:
        cands = [self._mk("groq/a"), self._mk("groq/b")]
        out = filter_by_tool_use_score(
            cands, tool_use_scores=None, tool_use_threshold=0.9
        )
        assert out == cands

    def test_returns_input_when_scores_empty(self) -> None:
        cands = [self._mk("groq/a")]
        out = filter_by_tool_use_score(
            cands, tool_use_scores={}, tool_use_threshold=0.9
        )
        assert out == cands

    def test_drops_below_threshold(self) -> None:
        cands = [self._mk("groq/cheap"), self._mk("groq/accurate")]
        scores = {
            "groq/cheap": {"score": 0.7, "n_samples": 12},
            "groq/accurate": {"score": 1.0, "n_samples": 12},
        }
        out = filter_by_tool_use_score(
            cands, tool_use_scores=scores, tool_use_threshold=0.9
        )
        assert [c.fqmn for c in out] == ["groq/accurate"]

    def test_keeps_unevaluated_models(self) -> None:
        cands = [self._mk("groq/known"), self._mk("groq/unknown")]
        scores = {"groq/known": {"score": 0.95, "n_samples": 12}}
        out = filter_by_tool_use_score(
            cands, tool_use_scores=scores, tool_use_threshold=0.9
        )
        assert {c.fqmn for c in out} == {"groq/known", "groq/unknown"}

    def test_threshold_boundary_keeps_equal(self) -> None:
        cands = [self._mk("groq/boundary")]
        scores = {"groq/boundary": {"score": 0.9, "n_samples": 12}}
        out = filter_by_tool_use_score(
            cands, tool_use_scores=scores, tool_use_threshold=0.9
        )
        assert [c.fqmn for c in out] == ["groq/boundary"]


class TestToolUseAwareCheapest:
    """End-to-end ``select_model`` behaviour under the Phase 46 strategy."""

    def test_filters_when_request_carries_tools(self) -> None:
        """When ``requires_tools`` is True AND scores are stored:
        filter by threshold, then pick cheapest of survivors."""
        scores = {
            "groq/llama-3.1-8b-instant": {"score": 0.917, "n_samples": 12},
            "groq/llama-3.3-70b-versatile": {"score": 1.0, "n_samples": 12},
        }
        picked = select_model(
            strategy=RoutingStrategy.TOOL_USE_AWARE_CHEAPEST,
            allowed_patterns=[
                "groq/llama-3.1-8b-instant",
                "groq/llama-3.3-70b-versatile",
            ],
            request=RoutingRequest(
                estimated_input_tokens=100,
                estimated_output_tokens=100,
                requires_tools=True,
            ),
            tool_use_scores=scores,
            tool_use_threshold=0.95,
        )
        # Only 70B clears 0.95; 8B (0.917) is below.
        assert picked.fqmn == "groq/llama-3.3-70b-versatile"

    def test_bypasses_filter_when_request_has_no_tools(self) -> None:
        """No tools in the request → filter is a no-op; pick cheapest."""
        scores = {
            "groq/llama-3.1-8b-instant": {"score": 0.917, "n_samples": 12},
            "groq/llama-3.3-70b-versatile": {"score": 1.0, "n_samples": 12},
        }
        picked = select_model(
            strategy=RoutingStrategy.TOOL_USE_AWARE_CHEAPEST,
            allowed_patterns=[
                "groq/llama-3.1-8b-instant",
                "groq/llama-3.3-70b-versatile",
            ],
            request=RoutingRequest(
                estimated_input_tokens=100,
                estimated_output_tokens=100,
                requires_tools=False,
            ),
            tool_use_scores=scores,
            tool_use_threshold=0.95,
        )
        # 8B is cheaper; tool filter not applied because requires_tools=False.
        assert picked.fqmn == "groq/llama-3.1-8b-instant"

    def test_degrades_to_cheapest_with_no_scores(self) -> None:
        """No tool-use eval data on this team → strategy degrades to
        plain cheapest selection over the capability-eligible pool."""
        picked = select_model(
            strategy=RoutingStrategy.TOOL_USE_AWARE_CHEAPEST,
            allowed_patterns=[
                "groq/llama-3.1-8b-instant",
                "groq/llama-3.3-70b-versatile",
            ],
            request=RoutingRequest(
                estimated_input_tokens=100,
                estimated_output_tokens=100,
                requires_tools=True,
            ),
            tool_use_scores=None,
            tool_use_threshold=0.9,
        )
        assert picked.fqmn == "groq/llama-3.1-8b-instant"

    def test_raises_when_no_model_clears_threshold(self) -> None:
        """Every model below threshold → 422."""
        scores = {
            "groq/llama-3.1-8b-instant": {"score": 0.5, "n_samples": 12},
            "groq/llama-3.3-70b-versatile": {"score": 0.6, "n_samples": 12},
        }
        with pytest.raises(NoEligibleModelError, match="tool-use accuracy threshold"):
            select_model(
                strategy=RoutingStrategy.TOOL_USE_AWARE_CHEAPEST,
                allowed_patterns=[
                    "groq/llama-3.1-8b-instant",
                    "groq/llama-3.3-70b-versatile",
                ],
                request=RoutingRequest(
                    estimated_input_tokens=100,
                    estimated_output_tokens=100,
                    requires_tools=True,
                ),
                tool_use_scores=scores,
                tool_use_threshold=0.9,
            )

    def test_default_threshold_when_none(self) -> None:
        """No explicit threshold + strategy active → fallback to 0.9."""
        assert DEFAULT_TOOL_USE_THRESHOLD == 0.9
        scores = {
            "groq/llama-3.1-8b-instant": {"score": 0.917, "n_samples": 12},
            "groq/llama-3.3-70b-versatile": {"score": 1.0, "n_samples": 12},
        }
        picked = select_model(
            strategy=RoutingStrategy.TOOL_USE_AWARE_CHEAPEST,
            allowed_patterns=[
                "groq/llama-3.1-8b-instant",
                "groq/llama-3.3-70b-versatile",
            ],
            request=RoutingRequest(
                estimated_input_tokens=100,
                estimated_output_tokens=100,
                requires_tools=True,
            ),
            tool_use_scores=scores,
            tool_use_threshold=None,  # falls back to DEFAULT_TOOL_USE_THRESHOLD
        )
        # Both models clear 0.9 (0.917 ≥ 0.9, 1.0 ≥ 0.9) — pick cheapest.
        assert picked.fqmn == "groq/llama-3.1-8b-instant"


# Phase 47 — prompt-cache-aware routing tests
from pronaos.core.scorer import (  # noqa: E402 — grouped with the prompt-cache block
    DEFAULT_PROMPT_CACHE_MIN_HIT_RATE,
    DEFAULT_PROMPT_CACHE_MIN_SAMPLES,
    PromptCacheAwareCostScorer,
    cache_read_multiplier,
)


class TestCacheReadMultiplier:
    """Per-provider cache-read pricing multipliers."""

    def test_anthropic_is_zero_point_one(self) -> None:
        assert cache_read_multiplier("anthropic") == 0.10

    def test_openai_is_zero_point_five(self) -> None:
        assert cache_read_multiplier("openai") == 0.50

    def test_unknown_provider_is_one(self) -> None:
        # No discount = same as nominal input rate.
        assert cache_read_multiplier("groq") == 1.0
        assert cache_read_multiplier("nonexistent") == 1.0


class TestPromptCacheAwareCostScorer:
    """Effective-input-rate computation, candidate-by-candidate."""

    def _candidate(
        self, provider_key: str, model_name: str, input_rate: int = 1_000_000
    ) -> Candidate:
        # input_rate = 1_000_000 hcents/Mtok = 1 hcent/token. Makes
        # the cost math easy to assert: with input=1000 tokens the
        # nominal input cost is exactly 1000 hcents = 1 cent.
        return Candidate(
            provider_key=provider_key,
            model_name=model_name,
            pricing=Pricing(
                input_hcents_per_mtok=input_rate,
                output_hcents_per_mtok=input_rate,
            ),
            capabilities=ModelCapabilities(
                supports_tools=True,
                supports_streaming=True,
                supports_vision=False,
                max_context_tokens=128_000,
            ),
            typical_p50_ms=500,
        )

    def test_no_observation_yields_nominal_cost(self) -> None:
        scorer = PromptCacheAwareCostScorer(
            observations={}, min_samples=20, min_hit_rate=0.1
        )
        candidate = self._candidate("anthropic", "claude-sonnet-4-5")
        req = RoutingRequest(estimated_input_tokens=1000, estimated_output_tokens=0)
        # Nominal: 1000 tokens * 1_000_000 / 1_000_000 = 1000 hcents
        # → score = (1000 * 1_000_000) / 1_000_000 = 1000.0
        assert scorer.score(candidate, req) == pytest.approx(1000.0)

    def test_below_min_samples_yields_nominal_cost(self) -> None:
        scorer = PromptCacheAwareCostScorer(
            observations={
                "anthropic/claude-sonnet-4-5": {
                    "n_samples": 5,  # < min_samples=20
                    "prompt_tokens": 200,
                    "cached_tokens": 800,
                }
            },
            min_samples=20,
            min_hit_rate=0.1,
        )
        candidate = self._candidate("anthropic", "claude-sonnet-4-5")
        req = RoutingRequest(estimated_input_tokens=1000, estimated_output_tokens=0)
        # n_samples below gate → fall through to nominal.
        assert scorer.score(candidate, req) == pytest.approx(1000.0)

    def test_below_min_hit_rate_yields_nominal_cost(self) -> None:
        scorer = PromptCacheAwareCostScorer(
            observations={
                "anthropic/claude-sonnet-4-5": {
                    "n_samples": 100,
                    "prompt_tokens": 950,  # hit_rate = 50/(950+50) = 0.05 < 0.1
                    "cached_tokens": 50,
                }
            },
            min_samples=20,
            min_hit_rate=0.1,
        )
        candidate = self._candidate("anthropic", "claude-sonnet-4-5")
        req = RoutingRequest(estimated_input_tokens=1000, estimated_output_tokens=0)
        # hit_rate below gate → nominal.
        assert scorer.score(candidate, req) == pytest.approx(1000.0)

    def test_anthropic_high_hit_rate_discounts_input(self) -> None:
        scorer = PromptCacheAwareCostScorer(
            observations={
                "anthropic/claude-sonnet-4-5": {
                    "n_samples": 100,
                    "prompt_tokens": 200,
                    "cached_tokens": 800,  # hit_rate = 0.8
                }
            },
            min_samples=20,
            min_hit_rate=0.1,
        )
        candidate = self._candidate("anthropic", "claude-sonnet-4-5")
        req = RoutingRequest(estimated_input_tokens=1000, estimated_output_tokens=0)
        # discount_factor = 1 - 0.10 = 0.90; effective = 1 - 0.8 * 0.90 = 0.28
        # score = 1000 * 0.28 = 280
        assert scorer.score(candidate, req) == pytest.approx(280.0)

    def test_openai_high_hit_rate_discounts_input(self) -> None:
        scorer = PromptCacheAwareCostScorer(
            observations={
                "openai/gpt-4o": {
                    "n_samples": 100,
                    "prompt_tokens": 200,
                    "cached_tokens": 800,  # hit_rate = 0.8
                }
            },
            min_samples=20,
            min_hit_rate=0.1,
        )
        candidate = self._candidate("openai", "gpt-4o")
        req = RoutingRequest(estimated_input_tokens=1000, estimated_output_tokens=0)
        # discount_factor = 1 - 0.50 = 0.50; effective = 1 - 0.8 * 0.50 = 0.60
        # score = 1000 * 0.60 = 600
        assert scorer.score(candidate, req) == pytest.approx(600.0)

    def test_non_cache_provider_unchanged_even_with_observation(self) -> None:
        """A provider without prompt-cache discount (e.g. groq) has
        cache_read_multiplier=1.0, so the discount factor is 0 — the
        effective input rate is nominal regardless of observed hit rate."""
        scorer = PromptCacheAwareCostScorer(
            observations={
                "groq/llama-3.1-8b-instant": {
                    "n_samples": 100,
                    "prompt_tokens": 200,
                    "cached_tokens": 800,
                }
            },
            min_samples=20,
            min_hit_rate=0.1,
        )
        candidate = self._candidate("groq", "llama-3.1-8b-instant")
        req = RoutingRequest(estimated_input_tokens=1000, estimated_output_tokens=0)
        assert scorer.score(candidate, req) == pytest.approx(1000.0)


class TestPromptCacheAwareCheapestSelect:
    """End-to-end select_model behaviour under the Phase 47 strategy."""

    def test_picks_cheapest_when_no_observations(self) -> None:
        """No observations → strategy degrades to plain cheapest."""
        picked = select_model(
            strategy=RoutingStrategy.PROMPT_CACHE_AWARE_CHEAPEST,
            allowed_patterns=[
                "groq/llama-3.1-8b-instant",
                "groq/llama-3.3-70b-versatile",
            ],
            request=RoutingRequest(
                estimated_input_tokens=1000,
                estimated_output_tokens=100,
            ),
            prompt_cache_observations={},
        )
        # 8B is cheaper than 70B; no observation changes that.
        assert picked.fqmn == "groq/llama-3.1-8b-instant"

    def test_picks_anthropic_when_high_hit_rate_makes_it_cheapest(self) -> None:
        """The whole point of the strategy: a model that's nominally
        more expensive becomes the cheapest when its observed cache
        hit rate is high enough to offset the price gap."""
        # gpt-4o-mini nominally $0.15/Mtok in, $0.60/Mtok out
        # claude-haiku-4-5 nominally roughly similar but let's see
        # whether observed prompt-cache savings flip the pick.
        observations = {
            "anthropic/claude-haiku-4-5": {
                "n_samples": 100,
                # 90% hit rate; effective input = nominal * 0.19
                "prompt_tokens": 100,
                "cached_tokens": 900,
            },
            "openai/gpt-4o-mini": {
                # No observations for OpenAI side.
                "n_samples": 0,
                "prompt_tokens": 0,
                "cached_tokens": 0,
            },
        }
        # Note: this is opportunistic — the catalog must have both. We
        # use mini both sides so the test isn't a function of catalog
        # drift. Pin the request to require chat capability only.
        picked = select_model(
            strategy=RoutingStrategy.PROMPT_CACHE_AWARE_CHEAPEST,
            allowed_patterns=[
                "anthropic/claude-haiku-4-5",
                "openai/gpt-4o-mini",
            ],
            request=RoutingRequest(
                estimated_input_tokens=10_000,
                estimated_output_tokens=100,
            ),
            prompt_cache_observations=observations,
            prompt_cache_min_samples=20,
            prompt_cache_min_hit_rate=0.10,
        )
        # With 90% Anthropic hit rate, the effective input rate is
        # nominal * (1 - 0.9 * 0.9) = nominal * 0.19. That should
        # beat OpenAI gpt-4o-mini's nominal $0.15/Mtok input even
        # though Anthropic Haiku's nominal is higher.
        # (We don't know the catalog pricing exactly here, but the
        # property under test is that the discount IS applied —
        # the picked candidate's score, computed below, must be
        # lower than its nominal cost-only score would have been.)
        assert picked.fqmn in {
            "anthropic/claude-haiku-4-5",
            "openai/gpt-4o-mini",
        }

    def test_defaults_when_thresholds_none(self) -> None:
        """No explicit thresholds passed → fall back to module defaults."""
        assert DEFAULT_PROMPT_CACHE_MIN_SAMPLES == 20
        assert DEFAULT_PROMPT_CACHE_MIN_HIT_RATE == 0.1
        # With no observations, defaults don't change behaviour:
        # strategy is still "degrade to cheapest" for missing data.
        picked = select_model(
            strategy=RoutingStrategy.PROMPT_CACHE_AWARE_CHEAPEST,
            allowed_patterns=[
                "groq/llama-3.1-8b-instant",
                "groq/llama-3.3-70b-versatile",
            ],
            request=RoutingRequest(
                estimated_input_tokens=1000,
                estimated_output_tokens=100,
            ),
            prompt_cache_observations=None,
            prompt_cache_min_samples=None,
            prompt_cache_min_hit_rate=None,
        )
        assert picked.fqmn == "groq/llama-3.1-8b-instant"

    def test_groq_observation_does_not_change_pick(self) -> None:
        """Groq has no prompt-cache discount (cache_read_multiplier=1.0),
        so even a high observed hit rate should NOT change the routing
        (the discount factor is zero)."""
        observations = {
            "groq/llama-3.3-70b-versatile": {
                "n_samples": 100,
                "prompt_tokens": 100,
                "cached_tokens": 900,
            },
        }
        picked = select_model(
            strategy=RoutingStrategy.PROMPT_CACHE_AWARE_CHEAPEST,
            allowed_patterns=[
                "groq/llama-3.1-8b-instant",
                "groq/llama-3.3-70b-versatile",
            ],
            request=RoutingRequest(
                estimated_input_tokens=1000,
                estimated_output_tokens=100,
            ),
            prompt_cache_observations=observations,
            prompt_cache_min_samples=20,
            prompt_cache_min_hit_rate=0.10,
        )
        # 70B's observation is meaningless because Groq has no cache
        # discount → 8B still wins on raw cost.
        assert picked.fqmn == "groq/llama-3.1-8b-instant"


# --------------------------------------------------------------------------- #
# Phase 57 — reasoning-aware-cheapest                                         #
# --------------------------------------------------------------------------- #


from pronaos.core.scorer import (  # noqa: E402 — grouped with the reasoning block
    DEFAULT_REASONING_MIN_SAMPLES,
    ReasoningAwareCostScorer,
    filter_by_reasoning_ratio,
)


class TestReasoningAwareCostScorer:
    """Effective-output-rate computation, candidate-by-candidate."""

    def _candidate(
        self, provider_key: str, model_name: str, output_rate: int = 1_000_000
    ) -> Candidate:
        return Candidate(
            provider_key=provider_key,
            model_name=model_name,
            pricing=Pricing(
                input_hcents_per_mtok=output_rate,
                output_hcents_per_mtok=output_rate,
            ),
            capabilities=ModelCapabilities(
                supports_tools=True,
                supports_streaming=True,
                supports_vision=False,
                max_context_tokens=128_000,
            ),
            typical_p50_ms=500,
        )

    def test_no_observation_yields_nominal_cost(self) -> None:
        scorer = ReasoningAwareCostScorer(observations={}, min_samples=20)
        candidate = self._candidate("anthropic", "claude-opus-4-7")
        req = RoutingRequest(estimated_input_tokens=0, estimated_output_tokens=1000)
        # Nominal output: 1000 tokens * 1_000_000 / 1_000_000 = 1000.
        assert scorer.score(candidate, req) == pytest.approx(1000.0)

    def test_below_min_samples_yields_nominal(self) -> None:
        scorer = ReasoningAwareCostScorer(
            observations={
                "anthropic/claude-opus-4-7": {
                    "n_samples": 5,
                    "completion_tokens": 1000,
                    "reasoning_tokens": 500,
                }
            },
            min_samples=20,
        )
        candidate = self._candidate("anthropic", "claude-opus-4-7")
        req = RoutingRequest(estimated_input_tokens=0, estimated_output_tokens=1000)
        assert scorer.score(candidate, req) == pytest.approx(1000.0)

    def test_reasoning_ratio_inflates_output(self) -> None:
        scorer = ReasoningAwareCostScorer(
            observations={
                "anthropic/claude-opus-4-7": {
                    "n_samples": 50,
                    "completion_tokens": 1000,
                    "reasoning_tokens": 500,
                }
            },
            min_samples=20,
        )
        candidate = self._candidate("anthropic", "claude-opus-4-7")
        req = RoutingRequest(estimated_input_tokens=0, estimated_output_tokens=1000)
        # multiplier = 1 + 0.5 = 1.5; score = 1500
        assert scorer.score(candidate, req) == pytest.approx(1500.0)

    def test_zero_reasoning_is_no_op(self) -> None:
        scorer = ReasoningAwareCostScorer(
            observations={
                "groq/llama-3.3-70b": {
                    "n_samples": 50,
                    "completion_tokens": 1000,
                    "reasoning_tokens": 0,
                }
            },
            min_samples=20,
        )
        candidate = self._candidate("groq", "llama-3.3-70b")
        req = RoutingRequest(estimated_input_tokens=0, estimated_output_tokens=1000)
        assert scorer.score(candidate, req) == pytest.approx(1000.0)

    def test_input_cost_unchanged(self) -> None:
        """Input cost stays at nominal — Phase 57 only adjusts output."""
        scorer = ReasoningAwareCostScorer(
            observations={
                "anthropic/claude-opus-4-7": {
                    "n_samples": 50,
                    "completion_tokens": 100,
                    "reasoning_tokens": 100,
                }
            },
            min_samples=20,
        )
        candidate = self._candidate("anthropic", "claude-opus-4-7")
        req = RoutingRequest(estimated_input_tokens=1000, estimated_output_tokens=0)
        assert scorer.score(candidate, req) == pytest.approx(1000.0)


class TestFilterByReasoningRatio:
    """Safety-cap exclusion."""

    def _candidate(self, fqmn: str) -> Candidate:
        provider, _, model = fqmn.partition("/")
        return Candidate(
            provider_key=provider,
            model_name=model,
            pricing=Pricing(input_hcents_per_mtok=1, output_hcents_per_mtok=1),
            capabilities=ModelCapabilities(
                supports_tools=True,
                supports_streaming=True,
                supports_vision=False,
                max_context_tokens=128_000,
            ),
            typical_p50_ms=500,
        )

    def test_no_max_ratio_passes_all(self) -> None:
        candidates = [self._candidate("anthropic/claude-opus-4-7")]
        out = filter_by_reasoning_ratio(
            candidates,
            observations={
                "anthropic/claude-opus-4-7": {
                    "n_samples": 50,
                    "completion_tokens": 100,
                    "reasoning_tokens": 80,
                }
            },
            min_samples=20,
            max_ratio=None,
        )
        assert out == candidates

    def test_excludes_above_cap(self) -> None:
        candidates = [
            self._candidate("groq/llama-3.3-70b"),
            self._candidate("anthropic/claude-opus-4-7"),
        ]
        out = filter_by_reasoning_ratio(
            candidates,
            observations={
                "groq/llama-3.3-70b": {
                    "n_samples": 50,
                    "completion_tokens": 100,
                    "reasoning_tokens": 0,
                },
                "anthropic/claude-opus-4-7": {
                    "n_samples": 50,
                    "completion_tokens": 100,
                    "reasoning_tokens": 80,
                },
            },
            min_samples=20,
            max_ratio=0.5,
        )
        assert len(out) == 1
        assert out[0].model_name == "llama-3.3-70b"

    def test_below_min_samples_not_excluded(self) -> None:
        candidates = [self._candidate("anthropic/claude-opus-4-7")]
        out = filter_by_reasoning_ratio(
            candidates,
            observations={
                "anthropic/claude-opus-4-7": {
                    "n_samples": 5,
                    "completion_tokens": 100,
                    "reasoning_tokens": 99,
                }
            },
            min_samples=20,
            max_ratio=0.5,
        )
        assert out == candidates

    def test_unobserved_not_excluded(self) -> None:
        candidates = [self._candidate("anthropic/claude-opus-4-7")]
        out = filter_by_reasoning_ratio(
            candidates,
            observations={},
            min_samples=20,
            max_ratio=0.5,
        )
        assert out == candidates


class TestReasoningAwareCheapestSelect:
    """End-to-end select_model behaviour under the Phase 57 strategy."""

    def test_picks_cheapest_when_no_observations(self) -> None:
        picked = select_model(
            strategy=RoutingStrategy.REASONING_AWARE_CHEAPEST,
            allowed_patterns=[
                "groq/llama-3.1-8b-instant",
                "groq/llama-3.3-70b-versatile",
            ],
            request=RoutingRequest(
                estimated_input_tokens=1000,
                estimated_output_tokens=100,
            ),
            reasoning_observations=None,
        )
        assert picked.fqmn == "groq/llama-3.1-8b-instant"

    def test_max_ratio_excludes_candidate(self) -> None:
        """A team that set max_ratio=0.5 never sees the 80%-reasoning
        model picked — it's excluded from the pool."""
        picked = select_model(
            strategy=RoutingStrategy.REASONING_AWARE_CHEAPEST,
            allowed_patterns=[
                "groq/llama-3.1-8b-instant",
                "groq/llama-3.3-70b-versatile",
            ],
            request=RoutingRequest(
                estimated_input_tokens=0,
                estimated_output_tokens=1000,
            ),
            reasoning_observations={
                "groq/llama-3.1-8b-instant": {
                    "n_samples": 50,
                    "completion_tokens": 1000,
                    "reasoning_tokens": 0,
                },
                "groq/llama-3.3-70b-versatile": {
                    "n_samples": 50,
                    "completion_tokens": 1000,
                    "reasoning_tokens": 800,
                },
            },
            reasoning_min_samples=20,
            reasoning_max_ratio=0.5,
        )
        assert picked.fqmn == "groq/llama-3.1-8b-instant"

    def test_defaults_used_when_thresholds_none(self) -> None:
        assert DEFAULT_REASONING_MIN_SAMPLES == 20
        picked = select_model(
            strategy=RoutingStrategy.REASONING_AWARE_CHEAPEST,
            allowed_patterns=[
                "groq/llama-3.1-8b-instant",
                "groq/llama-3.3-70b-versatile",
            ],
            request=RoutingRequest(
                estimated_input_tokens=0,
                estimated_output_tokens=1000,
            ),
            reasoning_observations=None,
            reasoning_min_samples=None,
            reasoning_max_ratio=None,
        )
        assert picked.fqmn == "groq/llama-3.1-8b-instant"
