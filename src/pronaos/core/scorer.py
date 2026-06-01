"""Model scoring for cost-aware routing (Phase 21).

When a client sends ``model="auto"``, the gateway must pick a concrete
``provider/model`` to dispatch to. The selection algorithm is:

    1. Build the pool of *candidate* (provider, model) pairs the team's
       allowlist permits.
    2. Filter candidates by the request's *capability requirements*
       (tools needed → must support tools; vision input → must support
       vision; estimated input tokens > max_context → ineligible).
    3. Score each eligible candidate with a ``ModelScorer`` chosen by
       the team's ``routing_strategy``.
    4. Return the lowest-scored candidate.

The scorer is pure and synchronous — it doesn't do I/O. Pricing and
capability lookups happen against the in-memory ``CATALOG``. This keeps
the routing decision sub-millisecond on the hot path.

Strategies
----------
- ``CHEAPEST``: minimise expected cost in hundredths-of-a-cent for the
  estimated token budget.
- ``FASTEST``: minimise expected wire latency using ``typical_p50_ms``
  from the catalog (best-effort; refine with real measurements over time).
- ``BALANCED``: normalised cost * normalised latency on a 0..1 scale per
  axis, summed. Lets fast-and-cheap providers beat the extreme on either
  axis.

Why not "best quality": the gateway doesn't have a quality model online.
Quality-aware routing is a separate layer that needs eval scores per model
per workload; that's roadmap, not Phase 21.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from pronaos.providers.catalog import (
    CATALOG,
    ModelCapabilities,
    get_capabilities,
)
from pronaos.providers.openai_compat import Pricing

# --------------------------------------------------------------------------- #
# Strategy enum                                                               #
# --------------------------------------------------------------------------- #


class RoutingStrategy(StrEnum):
    """How auto-routing should rank eligible models.

    Values are the wire format used in the ``teams.routing_strategy`` column
    and in the admin API. ``StrEnum`` so JSON serialisation is the string,
    not the integer ordinal.
    """

    CHEAPEST = "cheapest"
    FASTEST = "fastest"
    BALANCED = "balanced"
    # Phase 24: filter by a quality threshold (using per-model eval scores
    # stored on the team) before picking the cheapest of what remains.
    # When the team has no eval scores stored, degrades to plain ``cheapest``.
    QUALITY_AWARE_CHEAPEST = "quality-aware-cheapest"
    # Phase 46: filter by a per-model tool-use-accuracy threshold (BFCL-style
    # eval scores from Phase 45) WHEN the request carries tools. Tool-less
    # requests degrade to plain ``cheapest``. When the team has no
    # ``tool_use_scores`` stored, also degrades to ``cheapest`` — the
    # router doesn't penalise unevaluated models.
    TOOL_USE_AWARE_CHEAPEST = "tool-use-aware-cheapest"
    # Phase 47: score by *expected* cost given the team's per-model
    # observed prompt-cache hit rate. Models with high hit rates on this
    # team's traffic get their input cost discounted by
    # ``hit_rate * (1 - cache_read_multiplier)`` before the cheapest pick.
    # Anthropic (cache reads 0.10x) and OpenAI (0.50x) are the providers
    # where this matters today; others have cache_read_multiplier=1.0
    # (no discount) so the strategy degrades to plain ``cheapest`` for them.
    # When NO model on the team has crossed the min_samples + min_hit_rate
    # gate, the strategy degrades to plain ``cheapest`` end-to-end.
    PROMPT_CACHE_AWARE_CHEAPEST = "prompt-cache-aware-cheapest"
    # Phase 57: score by *expected* cost given the team's per-model
    # observed reasoning-token ratio. Reasoning content is billed at
    # the output rate (Anthropic counts thinking IN output_tokens;
    # OpenAI/DeepSeek count reasoning IN completion_tokens; Vertex
    # Gemini's adapter ADDS thoughtsTokenCount to completion_tokens
    # — see Phase 56). So a model that burns 50% of its output as
    # reasoning costs 1.5x its nominal output rate on real workloads.
    # The scorer multiplies each candidate's output rate by
    # ``1 + observed_ratio`` before picking the cheapest.
    # Optionally excludes candidates whose observed ratio exceeds
    # ``reasoning_max_ratio`` (per-team safety cap).
    # Degrades to plain ``cheapest`` when no fqmn has crossed
    # ``min_samples`` — the rolling mean isn't trustworthy yet.
    REASONING_AWARE_CHEAPEST = "reasoning-aware-cheapest"


# Sentinel "fast tier" latency for providers with no published p50 — we
# don't want them to win the latency race by accident.
_UNKNOWN_LATENCY_MS = 5_000

# Default quality threshold when the team has ``quality-aware-cheapest``
# selected but no explicit ``quality_threshold`` value stored. Matches
# the eval harness's default pass-threshold.
DEFAULT_QUALITY_THRESHOLD = 0.7

# Default tool-use-accuracy threshold when the team has
# ``tool-use-aware-cheapest`` selected but no explicit
# ``tool_use_threshold`` stored. Higher than the quality default (0.7)
# because tool-use sloppiness is operationally costly — a wrong tool
# arg breaks an agent loop end-to-end. Operators can tune per-team.
DEFAULT_TOOL_USE_THRESHOLD = 0.9

# Phase 47 defaults for ``prompt-cache-aware-cheapest`` when the team
# has the strategy enabled but no explicit thresholds stored. 20 samples
# is "enough to trust the rolling mean isn't a one-off"; 0.10 hit rate
# is "the savings adjustment is meaningful enough to matter for routing"
# — below ~10%, the cheaper-base-rate model usually still wins anyway.
DEFAULT_PROMPT_CACHE_MIN_SAMPLES = 20
DEFAULT_PROMPT_CACHE_MIN_HIT_RATE = 0.1

# Phase 57 defaults for ``reasoning-aware-cheapest``. 20 samples
# mirrors Phase 47 — "enough to trust the rolling mean isn't a
# one-off." No default max-ratio cap; operators set it explicitly
# when they want to exclude reasoning-heavy models.
DEFAULT_REASONING_MIN_SAMPLES = 20

# Per-provider cache-read pricing multiplier. The fraction of the
# provider's nominal input rate that cache reads are billed at:
#   * Anthropic: 0.10 (90% discount) — Phase 34
#   * OpenAI:    0.50 (50% discount) — Phase 35
#   * Everyone else: 1.0 (no discount; observer's hit_rate naturally ~0)
# Kept in this module rather than per-provider so the scorer stays a
# pure function — no provider-class lookups on the hot routing path.
_CACHE_READ_MULTIPLIER: dict[str, float] = {
    "anthropic": 0.10,
    "openai": 0.50,
}


def cache_read_multiplier(provider_key: str) -> float:
    """Return the input-rate fraction that cache reads cost on this
    provider. 1.0 (no discount) for providers without prompt caching
    so the prompt-cache-aware scorer's effective-cost math is a no-op
    for them."""
    return _CACHE_READ_MULTIPLIER.get(provider_key, 1.0)


# --------------------------------------------------------------------------- #
# Request + Candidate                                                         #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class RoutingRequest:
    """The per-request inputs the scorer needs.

    ``estimated_input_tokens`` and ``estimated_output_tokens`` come from
    the preflight estimator (Phase 20). The scorer doesn't re-estimate;
    it trusts whatever was already computed.
    """

    estimated_input_tokens: int
    estimated_output_tokens: int
    requires_tools: bool = False
    requires_vision: bool = False
    requires_streaming: bool = False


@dataclass(frozen=True, slots=True)
class Candidate:
    """One (provider, model) pair the router is considering."""

    provider_key: str
    model_name: str
    pricing: Pricing
    capabilities: ModelCapabilities
    typical_p50_ms: int

    @property
    def fqmn(self) -> str:
        """Fully-qualified model name in the routing-string form."""
        return f"{self.provider_key}/{self.model_name}"


# --------------------------------------------------------------------------- #
# Scorers                                                                     #
# --------------------------------------------------------------------------- #


class ModelScorer(Protocol):
    """Score a candidate against a request. **Lower is better.**

    Implementations must be deterministic (same inputs → same score) and
    pure (no I/O, no shared mutable state). This lets the selector cache
    decisions and lets tests assert exact scores.
    """

    def score(self, candidate: Candidate, request: RoutingRequest) -> float: ...


class CostScorer:
    """Score by expected dispatch cost in hundredths-of-a-cent.

    Computes ``(in_tokens * in_price + out_tokens * out_price) / 1e6``
    where prices are per million tokens. Returns a float (sub-hcent
    fractions matter when comparing very cheap models).
    """

    def score(self, candidate: Candidate, request: RoutingRequest) -> float:
        in_cost = request.estimated_input_tokens * candidate.pricing.input_hcents_per_mtok
        out_cost = request.estimated_output_tokens * candidate.pricing.output_hcents_per_mtok
        return (in_cost + out_cost) / 1_000_000.0


class PromptCacheAwareCostScorer:
    """Phase 47: cost scorer that discounts each candidate's input rate
    by ``observed_hit_rate * (1 - cache_read_multiplier)``.

    Inputs:
      * ``observations`` — ``{fqmn: PromptCacheStat-like-mapping}`` from
        the observer's snapshot. Each entry needs ``n_samples`` (int),
        ``prompt_tokens`` (int), ``cached_tokens`` (int). The scorer is
        defensive: missing fields treated as 0.
      * ``min_samples`` — candidates with fewer observations get scored
        on plain cost (the rolling mean isn't load-bearing yet).
      * ``min_hit_rate`` — candidates below this hit rate get scored on
        plain cost (the savings adjustment is in the noise).

    The result is comparable across providers — Anthropic with hit rate
    0.8 gets effective input cost = nominal * (1 - 0.8 * 0.9) = nominal *
    0.28; OpenAI with hit rate 0.8 gets nominal * 0.6; a no-cache provider
    is unchanged at nominal * 1.0. Output cost is always nominal.

    Stateless — instantiate once per request (cheap) or share globally.
    """

    def __init__(
        self,
        *,
        observations: dict[str, dict[str, int]],
        min_samples: int,
        min_hit_rate: float,
    ) -> None:
        self._observations = observations
        self._min_samples = min_samples
        self._min_hit_rate = min_hit_rate

    def score(self, candidate: Candidate, request: RoutingRequest) -> float:
        nominal_in = (
            request.estimated_input_tokens * candidate.pricing.input_hcents_per_mtok
        )
        out_cost = (
            request.estimated_output_tokens * candidate.pricing.output_hcents_per_mtok
        )
        multiplier = self._effective_input_multiplier(candidate)
        return (nominal_in * multiplier + out_cost) / 1_000_000.0

    def _effective_input_multiplier(self, candidate: Candidate) -> float:
        """Return ``1 - h * (1 - d)`` where h = observed hit rate, d =
        provider's cache_read_multiplier. Returns 1.0 (no discount) when
        the candidate hasn't crossed the sample / hit-rate gates."""
        entry = self._observations.get(candidate.fqmn)
        if not isinstance(entry, dict):
            return 1.0
        n_samples = int(entry.get("n_samples", 0) or 0)
        if n_samples < self._min_samples:
            return 1.0
        prompt_tokens = int(entry.get("prompt_tokens", 0) or 0)
        cached_tokens = int(entry.get("cached_tokens", 0) or 0)
        denom = prompt_tokens + cached_tokens
        if denom <= 0:
            return 1.0
        hit_rate = cached_tokens / denom
        if hit_rate < self._min_hit_rate:
            return 1.0
        discount_factor = 1.0 - cache_read_multiplier(candidate.provider_key)
        # discount_factor is the fraction of input cost saved per cached
        # token. effective rate = nominal * (1 - h * discount_factor).
        return max(0.0, 1.0 - hit_rate * discount_factor)


class ReasoningAwareCostScorer:
    """Phase 57: cost scorer that multiplies each candidate's output
    rate by ``1 + observed_reasoning_ratio``.

    Inputs:
      * ``observations`` — ``{fqmn: ReasoningStat-like-mapping}`` from
        the observer's snapshot. Each entry needs ``n_samples`` (int),
        ``completion_tokens`` (int), ``reasoning_tokens`` (int). The
        scorer is defensive: missing fields treated as 0.
      * ``min_samples`` — candidates with fewer observations get scored
        on plain cost (the rolling mean isn't load-bearing yet).

    The result is comparable across providers. A Claude 4 with 50%
    observed reasoning ratio gets effective output cost = nominal *
    1.5; a Gemini 2.5 Pro with 80% ratio (heavy thinking on this
    team's workload) gets nominal * 1.8; a non-reasoning Groq Llama
    with 0% ratio is unchanged at nominal * 1.0. Input cost is
    always nominal.

    Stateless — instantiate once per request (cheap) or share globally.
    """

    def __init__(
        self,
        *,
        observations: dict[str, dict[str, int]],
        min_samples: int,
    ) -> None:
        self._observations = observations
        self._min_samples = min_samples

    def score(self, candidate: Candidate, request: RoutingRequest) -> float:
        in_cost = (
            request.estimated_input_tokens * candidate.pricing.input_hcents_per_mtok
        )
        nominal_out = (
            request.estimated_output_tokens * candidate.pricing.output_hcents_per_mtok
        )
        multiplier = self._effective_output_multiplier(candidate)
        return (in_cost + nominal_out * multiplier) / 1_000_000.0

    def _effective_output_multiplier(self, candidate: Candidate) -> float:
        """Return ``1 + observed_ratio`` where observed_ratio =
        reasoning_tokens / completion_tokens. Returns 1.0 (no
        adjustment) when the candidate hasn't crossed the sample
        gate or has zero observed output."""
        entry = self._observations.get(candidate.fqmn)
        if not isinstance(entry, dict):
            return 1.0
        n_samples = int(entry.get("n_samples", 0) or 0)
        if n_samples < self._min_samples:
            return 1.0
        completion_tokens = int(entry.get("completion_tokens", 0) or 0)
        reasoning_tokens = int(entry.get("reasoning_tokens", 0) or 0)
        if completion_tokens <= 0:
            return 1.0
        ratio = reasoning_tokens / completion_tokens
        # No upper clamp — if a model genuinely burns 5x its visible
        # output on reasoning (extreme but possible on hard math
        # problems), the router should reflect that.
        return max(1.0, 1.0 + ratio)


def filter_by_reasoning_ratio(
    eligible: list[Candidate],
    *,
    observations: dict[str, dict[str, int]],
    min_samples: int,
    max_ratio: float | None,
) -> list[Candidate]:
    """Phase 57: exclude candidates whose observed reasoning ratio
    exceeds the team's safety cap.

    Operators who want to cap reasoning-heavy models out of the
    routing pool set ``max_ratio`` (e.g. 0.5 = "exclude models that
    spend more than 50% of output on reasoning"). When ``max_ratio``
    is None (the default), no exclusion happens.

    Candidates with fewer than ``min_samples`` observations are NEVER
    excluded — they pass through to the cost-ranking stage where the
    scorer scores them at nominal (no penalty). This avoids over-
    penalising new models the team just started using.
    """
    if max_ratio is None:
        return eligible
    out: list[Candidate] = []
    for c in eligible:
        entry = observations.get(c.fqmn)
        if not isinstance(entry, dict):
            out.append(c)
            continue
        n_samples = int(entry.get("n_samples", 0) or 0)
        if n_samples < min_samples:
            out.append(c)
            continue
        completion_tokens = int(entry.get("completion_tokens", 0) or 0)
        reasoning_tokens = int(entry.get("reasoning_tokens", 0) or 0)
        if completion_tokens <= 0:
            out.append(c)
            continue
        ratio = reasoning_tokens / completion_tokens
        if ratio <= max_ratio:
            out.append(c)
    return out


class FastestScorer:
    """Score by typical p50 latency.

    Ignores the request entirely — latency depends on the provider's
    typical response time, not on the prompt. This is a best-effort
    proxy until real per-request latency telemetry is wired in.
    """

    def score(self, candidate: Candidate, _request: RoutingRequest) -> float:
        return float(candidate.typical_p50_ms)


class BalancedScorer:
    """Score by normalised cost + normalised latency.

    Both axes are scaled to a 0..1 range using the candidate pool's
    min/max, then summed. This means BalancedScorer is *context-aware*
    — it can't score a single candidate in isolation; it scores the
    pool together. The selector calls ``score_pool`` rather than
    ``score`` for this strategy.
    """

    def score_pool(self, candidates: list[Candidate], request: RoutingRequest) -> list[float]:
        if not candidates:
            return []

        cost = CostScorer()
        fast = FastestScorer()
        costs = [cost.score(c, request) for c in candidates]
        lats = [fast.score(c, request) for c in candidates]

        cost_min, cost_max = min(costs), max(costs)
        lat_min, lat_max = min(lats), max(lats)
        cost_span = max(cost_max - cost_min, 1e-9)
        lat_span = max(lat_max - lat_min, 1e-9)

        return [
            ((c - cost_min) / cost_span) + ((latency_value - lat_min) / lat_span)
            for c, latency_value in zip(costs, lats, strict=True)
        ]


# --------------------------------------------------------------------------- #
# Selector                                                                    #
# --------------------------------------------------------------------------- #


class NoEligibleModelError(Exception):
    """No candidate from the team's allowlist satisfied the request."""


def build_candidates(
    *,
    allowed_patterns: Iterable[str] | None,
) -> list[Candidate]:
    """Enumerate every (provider, model) pair the team's allowlist permits.

    ``allowed_patterns`` is the team's ``allowed_models`` column — a list
    of fnmatch patterns matched against ``provider/model``. ``None`` means
    unrestricted (every catalog entry is in play); ``[]`` means deny-all
    (no candidates produced).

    Only catalog entries with a known ``pricing`` row are returned —
    auto-routing won't pick a model with unknown cost.
    """
    if allowed_patterns is not None and len(list(allowed_patterns)) == 0:
        return []

    patterns = list(allowed_patterns) if allowed_patterns is not None else None

    candidates: list[Candidate] = []
    for provider_key, entry in CATALOG.items():
        for model_name, pricing in entry.pricing.items():
            fqmn = f"{provider_key}/{model_name}"
            if patterns is not None and not any(fnmatch.fnmatch(fqmn, p) for p in patterns):
                continue
            caps = get_capabilities(provider_key, model_name)
            candidates.append(
                Candidate(
                    provider_key=provider_key,
                    model_name=model_name,
                    pricing=pricing,
                    capabilities=caps,
                    typical_p50_ms=entry.typical_p50_ms or _UNKNOWN_LATENCY_MS,
                )
            )
    return candidates


def filter_eligible(candidates: list[Candidate], request: RoutingRequest) -> list[Candidate]:
    """Drop candidates that can't satisfy the request's capability needs."""
    out: list[Candidate] = []
    for c in candidates:
        if request.requires_tools and not c.capabilities.supports_tools:
            continue
        if request.requires_vision and not c.capabilities.supports_vision:
            continue
        if request.requires_streaming and not c.capabilities.supports_streaming:
            continue
        # Context check: input tokens must fit. We use a 90% margin to
        # leave headroom for output tokens and the gateway's own overhead.
        if request.estimated_input_tokens > int(c.capabilities.max_context_tokens * 0.9):
            continue
        out.append(c)
    return out


def filter_by_tool_use_score(
    candidates: list[Candidate],
    *,
    tool_use_scores: dict[str, dict[str, Any]] | None,
    tool_use_threshold: float,
) -> list[Candidate]:
    """Drop candidates whose stored tool-use accuracy is below the threshold.

    Mirrors :func:`filter_by_quality` shape but reads ``tool_use_scores``
    (Phase 46) instead of ``quality_scores``. The same "unevaluated =
    keep" semantics apply: a model the team hasn't run the BFCL eval
    against is NOT excluded — operators can pin ``allowed_models`` if
    they want strict-eval-required behaviour.

    Caller is responsible for deciding WHEN to invoke this. The
    selector only calls it when the strategy is
    ``TOOL_USE_AWARE_CHEAPEST`` AND the request carries tools — see
    :func:`select_model`.
    """
    if not tool_use_scores:
        return candidates
    out: list[Candidate] = []
    for c in candidates:
        entry = tool_use_scores.get(c.fqmn)
        if entry is None:
            out.append(c)
            continue
        score = entry.get("score") if isinstance(entry, dict) else None
        if not isinstance(score, int | float):
            # Malformed entry — keep the candidate; admin endpoint
            # validates at write time so this is unlikely in practice.
            out.append(c)
            continue
        if float(score) >= tool_use_threshold:
            out.append(c)
    return out


def filter_by_quality(
    candidates: list[Candidate],
    *,
    quality_scores: dict[str, dict[str, Any]] | None,
    quality_threshold: float,
) -> list[Candidate]:
    """Drop candidates whose stored quality score is below the threshold.

    ``quality_scores`` is the team's ``quality_scores`` JSON column —
    a dict keyed by fully-qualified model name (``provider/model``)
    mapping to ``{"score": float, "n_samples": int, ...}``.

    Filtering semantics:

    - **Model present with score < threshold** → drop (we have evidence
      this model under-performs on this team's workload).
    - **Model present with score ≥ threshold** → keep.
    - **Model absent from scores** → **keep** (no evidence either way;
      we don't penalise unevaluated models or every new model gets
      excluded until eval runs catch up).

    When ``quality_scores`` is ``None`` or empty the function returns
    the input unchanged — the team has no eval data so there's nothing
    to filter on.
    """
    if not quality_scores:
        return candidates
    out: list[Candidate] = []
    for c in candidates:
        entry = quality_scores.get(c.fqmn)
        if entry is None:
            # Unevaluated — keep as a candidate. Operators who want
            # strict-eval-required behaviour can pin ``allowed_models``
            # to the evaluated set.
            out.append(c)
            continue
        score = entry.get("score") if isinstance(entry, dict) else None
        if not isinstance(score, int | float):
            # Malformed entry — be safe and keep the candidate. The
            # CLI / admin endpoint validates at write time so we
            # shouldn't see this in practice.
            out.append(c)
            continue
        if float(score) >= quality_threshold:
            out.append(c)
    return out


def select_model(
    *,
    strategy: RoutingStrategy,
    allowed_patterns: Iterable[str] | None,
    request: RoutingRequest,
    quality_scores: dict[str, dict[str, Any]] | None = None,
    quality_threshold: float | None = None,
    degraded_models_set: set[str] | None = None,
    tool_use_scores: dict[str, dict[str, Any]] | None = None,
    tool_use_threshold: float | None = None,
    prompt_cache_observations: dict[str, dict[str, int]] | None = None,
    prompt_cache_min_samples: int | None = None,
    prompt_cache_min_hit_rate: float | None = None,
    reasoning_observations: dict[str, dict[str, int]] | None = None,
    reasoning_min_samples: int | None = None,
    reasoning_max_ratio: float | None = None,
) -> Candidate:
    """Run the full selection pipeline and return the winning candidate.

    Raises :class:`NoEligibleModelError` if no candidate from the allowlist
    can satisfy the request — caller must surface this as 400/422 (it's a
    client problem, not a server problem).

    ``quality_scores`` + ``quality_threshold`` are only consulted when
    ``strategy == QUALITY_AWARE_CHEAPEST``. For other strategies they're
    ignored — the caller can always pass them without changing behaviour.

    ``degraded_models_set`` (Phase 40) lists fqmns the quality monitor
    has flagged as currently degraded. Applied AFTER capability + quality
    filtering and BEFORE the per-strategy ranking — degraded models are
    excluded from the candidate pool regardless of strategy. When every
    eligible model is degraded the function raises
    ``NoEligibleModelError`` so the caller can surface a clear 422.
    """
    pool = build_candidates(allowed_patterns=allowed_patterns)
    eligible = filter_eligible(pool, request)
    if not eligible:
        raise NoEligibleModelError(
            "no model in the team's allowlist satisfies the request's "
            f"requirements (tools={request.requires_tools}, "
            f"vision={request.requires_vision}, "
            f"streaming={request.requires_streaming}, "
            f"input_tokens={request.estimated_input_tokens})"
        )

    # Phase 40: filter out actively-degraded models. Applied across
    # all strategies (cheapest / fastest / balanced / quality-aware)
    # because "the model is broken" is orthogonal to "what's the
    # team's pricing preference."
    if degraded_models_set:
        eligible = [c for c in eligible if c.fqmn not in degraded_models_set]
        if not eligible:
            raise NoEligibleModelError(
                f"every eligible model in the team's allowlist is currently "
                f"marked as quality-degraded by the monitor ({sorted(degraded_models_set)}); "
                "wait for the monitor to detect recovery or widen the allowlist"
            )

    if strategy == RoutingStrategy.QUALITY_AWARE_CHEAPEST:
        # Second filter stage: quality threshold. If the team has no
        # scores stored, this is a no-op and we degrade to plain
        # ``cheapest`` selection over the capability-eligible pool.
        threshold = (
            quality_threshold if quality_threshold is not None else DEFAULT_QUALITY_THRESHOLD
        )
        quality_eligible = filter_by_quality(
            eligible,
            quality_scores=quality_scores,
            quality_threshold=threshold,
        )
        if not quality_eligible:
            # Every eligible model is below threshold. The honest
            # answer is "you have no model meeting your bar" — surface
            # as 422 so the operator either lowers the threshold or
            # widens the allowlist.
            raise NoEligibleModelError(
                f"no model in the team's allowlist meets the configured "
                f"quality threshold of {threshold} (consider lowering the "
                f"threshold or running eval on more models)"
            )
        # After quality filtering, pick the cheapest of what remains.
        scorer: ModelScorer = CostScorer()
        scores = [scorer.score(c, request) for c in quality_eligible]
        paired = sorted(
            zip(scores, quality_eligible, strict=True),
            key=lambda x: (x[0], x[1].fqmn),
        )
        return paired[0][1]

    # Phase 46: tool-use accuracy filter, but ONLY when the request
    # carries tools. Tool-less requests have nothing to gain from
    # filtering on tool-use accuracy — fall through to cheapest.
    if (
        strategy == RoutingStrategy.TOOL_USE_AWARE_CHEAPEST
        and request.requires_tools
    ):
        tu_threshold = (
            tool_use_threshold
            if tool_use_threshold is not None
            else DEFAULT_TOOL_USE_THRESHOLD
        )
        tu_eligible = filter_by_tool_use_score(
            eligible,
            tool_use_scores=tool_use_scores,
            tool_use_threshold=tu_threshold,
        )
        if not tu_eligible:
            raise NoEligibleModelError(
                f"no model in the team's allowlist meets the configured "
                f"tool-use accuracy threshold of {tu_threshold} "
                f"(consider lowering the threshold, running the BFCL "
                f"eval on more models, or widening the allowlist)"
            )
        tu_scorer: ModelScorer = CostScorer()
        tu_scores_for_pool = [tu_scorer.score(c, request) for c in tu_eligible]
        tu_paired = sorted(
            zip(tu_scores_for_pool, tu_eligible, strict=True),
            key=lambda x: (x[0], x[1].fqmn),
        )
        return tu_paired[0][1]
    # ``TOOL_USE_AWARE_CHEAPEST`` on a tool-less request falls through
    # to the cheapest branch below.

    # Phase 47: prompt-cache-aware cost scorer. Uses runtime-observed
    # per-model hit rates to discount each candidate's input rate
    # before picking the cheapest. Degrades to plain ``cheapest`` when
    # no observations have crossed the sample/hit-rate gates — the
    # discount multiplier just stays at 1.0 for everyone.
    if strategy == RoutingStrategy.PROMPT_CACHE_AWARE_CHEAPEST:
        pc_min_samples = (
            prompt_cache_min_samples
            if prompt_cache_min_samples is not None
            else DEFAULT_PROMPT_CACHE_MIN_SAMPLES
        )
        pc_min_hit_rate = (
            prompt_cache_min_hit_rate
            if prompt_cache_min_hit_rate is not None
            else DEFAULT_PROMPT_CACHE_MIN_HIT_RATE
        )
        pc_scorer = PromptCacheAwareCostScorer(
            observations=prompt_cache_observations or {},
            min_samples=pc_min_samples,
            min_hit_rate=pc_min_hit_rate,
        )
        pc_scores = [pc_scorer.score(c, request) for c in eligible]
        pc_paired = sorted(
            zip(pc_scores, eligible, strict=True),
            key=lambda x: (x[0], x[1].fqmn),
        )
        return pc_paired[0][1]

    # Phase 57: reasoning-aware cost scorer. Uses runtime-observed
    # per-model reasoning ratio to inflate each candidate's output
    # rate before picking the cheapest. Optional safety cap excludes
    # candidates whose ratio exceeds ``max_ratio``. Degrades to plain
    # ``cheapest`` when no observation has crossed ``min_samples``.
    if strategy == RoutingStrategy.REASONING_AWARE_CHEAPEST:
        r_min_samples = (
            reasoning_min_samples
            if reasoning_min_samples is not None
            else DEFAULT_REASONING_MIN_SAMPLES
        )
        r_observations = reasoning_observations or {}
        r_filtered = filter_by_reasoning_ratio(
            eligible,
            observations=r_observations,
            min_samples=r_min_samples,
            max_ratio=reasoning_max_ratio,
        )
        if not r_filtered:
            # Every observed model exceeded the cap. Surface as 422
            # so the operator either raises the cap or widens the
            # allowlist. The honest answer is "you asked us to
            # exclude these models AND your allowlist contains only
            # excluded models — we can't route."
            raise NoEligibleModelError(
                f"every eligible model exceeds the team's "
                f"reasoning-ratio cap of {reasoning_max_ratio} "
                "(consider raising the cap, widening the allowlist, "
                "or switching strategies)"
            )
        r_scorer = ReasoningAwareCostScorer(
            observations=r_observations,
            min_samples=r_min_samples,
        )
        r_scores = [r_scorer.score(c, request) for c in r_filtered]
        r_paired = sorted(
            zip(r_scores, r_filtered, strict=True),
            key=lambda x: (x[0], x[1].fqmn),
        )
        return r_paired[0][1]

    if strategy == RoutingStrategy.BALANCED:
        balanced_scores = BalancedScorer().score_pool(eligible, request)
    else:
        # CHEAPEST, FASTEST, and TOOL_USE_AWARE_CHEAPEST on a tool-less
        # request all fall through to a single-axis scorer. The
        # tool-less branch of TOOL_USE_AWARE_CHEAPEST degrades to
        # CHEAPEST — that's the documented contract.
        scorer_obj: ModelScorer = (
            FastestScorer() if strategy == RoutingStrategy.FASTEST else CostScorer()
        )
        balanced_scores = [scorer_obj.score(c, request) for c in eligible]

    # Stable tiebreak: lowest score wins; on ties, prefer the alphabetically
    # earlier fqmn so the choice is reproducible across processes.
    paired = sorted(
        zip(balanced_scores, eligible, strict=True),
        key=lambda x: (x[0], x[1].fqmn),
    )
    return paired[0][1]
