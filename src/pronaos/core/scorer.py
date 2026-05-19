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
from typing import Protocol

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


# Sentinel "fast tier" latency for providers with no published p50 — we
# don't want them to win the latency race by accident.
_UNKNOWN_LATENCY_MS = 5_000


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

    def score_pool(
        self, candidates: list[Candidate], request: RoutingRequest
    ) -> list[float]:
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
            if patterns is not None and not any(
                fnmatch.fnmatch(fqmn, p) for p in patterns
            ):
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


def filter_eligible(
    candidates: list[Candidate], request: RoutingRequest
) -> list[Candidate]:
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


def select_model(
    *,
    strategy: RoutingStrategy,
    allowed_patterns: Iterable[str] | None,
    request: RoutingRequest,
) -> Candidate:
    """Run the full selection pipeline and return the winning candidate.

    Raises :class:`NoEligibleModelError` if no candidate from the allowlist
    can satisfy the request — caller must surface this as 400/422 (it's a
    client problem, not a server problem).
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

    if strategy == RoutingStrategy.BALANCED:
        scores = BalancedScorer().score_pool(eligible, request)
    else:
        scorer: ModelScorer = (
            CostScorer() if strategy == RoutingStrategy.CHEAPEST else FastestScorer()
        )
        scores = [scorer.score(c, request) for c in eligible]

    # Stable tiebreak: lowest score wins; on ties, prefer the alphabetically
    # earlier fqmn so the choice is reproducible across processes.
    paired = sorted(
        zip(scores, eligible, strict=True), key=lambda x: (x[0], x[1].fqmn)
    )
    return paired[0][1]
