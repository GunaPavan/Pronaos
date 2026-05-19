"""Prometheus metrics for the gateway.

Conventions
-----------
- Metric names use the ``pronaos_`` prefix so they're trivially filterable
  in a multi-tenant Prometheus.
- We use a **dedicated registry** rather than ``REGISTRY`` (the default
  module-level singleton) so tests can construct/destroy state cleanly
  without ``Duplicated timeseries`` errors when the module re-imports.
- Histogram buckets are tuned for the gateway's expected latency profile
  (5 ms to 30 s — anything past 30 s is timed out by the failover layer
  before metrics land).

Cardinality
-----------
Labels are deliberately conservative:

- ``tenant_id`` / ``team_id`` / ``key_id`` are **not** on hot-path counters
  (HTTP requests, provider calls) — a tenant/team explosion would balloon
  series count. FinOps queries reach for the ``usage_records`` table
  instead, which is authoritative.
- ``provider`` / ``model`` are bounded by the catalog (~12 providers, dozens
  of models) so they're safe as labels.
- ``status_code`` / ``status`` are tiny enumerated sets.

This keeps the working-set series count in the low hundreds for a typical
deployment regardless of customer count.
"""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

# A dedicated registry — see module docstring for rationale.
REGISTRY = CollectorRegistry()


# --------------------------------------------------------------------------- #
# HTTP-level metrics                                                          #
# --------------------------------------------------------------------------- #

http_requests_total = Counter(
    "pronaos_http_requests_total",
    "Total HTTP requests served by the gateway.",
    labelnames=("method", "route", "status_code"),
    registry=REGISTRY,
)

# Histogram bucket choices reflect "5 ms cache hit … 30 s p99 cold provider
# call." Tighter low-end buckets matter more than the upper tail because
# anything north of 30 s is already an SLA breach worth alerting on.
http_request_duration_seconds = Histogram(
    "pronaos_http_request_duration_seconds",
    "End-to-end HTTP request duration in seconds.",
    labelnames=("method", "route"),
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
    registry=REGISTRY,
)


# --------------------------------------------------------------------------- #
# Provider-call metrics                                                       #
# --------------------------------------------------------------------------- #

provider_requests_total = Counter(
    "pronaos_provider_requests_total",
    "Upstream provider calls made by the gateway. ``status`` is success|error.",
    labelnames=("provider", "model", "status"),
    registry=REGISTRY,
)

provider_request_duration_seconds = Histogram(
    "pronaos_provider_request_duration_seconds",
    "Duration of a successful upstream provider call, seconds.",
    labelnames=("provider", "model"),
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 20.0, 30.0, 60.0),
    registry=REGISTRY,
)

provider_tokens_total = Counter(
    "pronaos_provider_tokens_total",
    "Tokens billed by providers. ``direction`` is prompt|completion.",
    labelnames=("provider", "model", "direction"),
    registry=REGISTRY,
)

provider_cost_hcents_total = Counter(
    "pronaos_provider_cost_hcents_total",
    "Cumulative cost in hundredths-of-a-cent (matches usage_records.cost_hcents).",
    labelnames=("provider", "model"),
    registry=REGISTRY,
)


# --------------------------------------------------------------------------- #
# Quota / rate-limit metrics                                                  #
# --------------------------------------------------------------------------- #

quota_denials_total = Counter(
    "pronaos_quota_denials_total",
    "Requests denied at the quota gate. ``reason`` distinguishes rate-limit "
    "from per-budget exhaustion.",
    labelnames=("reason",),
    registry=REGISTRY,
)


# --------------------------------------------------------------------------- #
# Cache metrics (Phase 7)                                                     #
# --------------------------------------------------------------------------- #

cache_lookups_total = Counter(
    "pronaos_cache_lookups_total",
    "Cache lookups. ``tier`` is exact|semantic, ``result`` is hit|miss|skip. "
    "``skip`` covers requests bypassed because of temperature>0, streaming, "
    "or an explicit bypass header.",
    labelnames=("tier", "result"),
    registry=REGISTRY,
)


# --------------------------------------------------------------------------- #
# Guardrail metrics (Phase 8)                                                 #
# --------------------------------------------------------------------------- #

guardrail_hits_total = Counter(
    "pronaos_guardrail_hits_total",
    "Guardrail rule firings. ``rule`` is the canonical rule name "
    "(e.g. pii.email, pii.ssn, injection); ``action`` is the action "
    "applied (block | redact | log_only); ``direction`` is ingress|egress.",
    labelnames=("rule", "action", "direction"),
    registry=REGISTRY,
)


# --------------------------------------------------------------------------- #
# Circuit-breaker metrics (Phase 15)                                          #
# --------------------------------------------------------------------------- #
#
# The breaker has cross-request state: dashboards want to read the *current*
# state (a gauge), and FinOps wants to count *events* — trips and the
# upstream calls those trips saved (counters). Three series total, each
# labelled by provider so a Grafana panel can split by upstream.

circuit_state = Gauge(
    "pronaos_circuit_state",
    "Current circuit breaker state per provider. "
    "0=closed (healthy), 1=half_open (probing), 2=open (tripped).",
    labelnames=("provider",),
    registry=REGISTRY,
)

circuit_trips_total = Counter(
    "pronaos_circuit_trips_total",
    "Number of times the circuit transitioned from CLOSED/HALF_OPEN to OPEN. "
    "A trip is a discrete event — a long outage adds 1, not many.",
    labelnames=("provider",),
    registry=REGISTRY,
)

circuit_skipped_requests_total = Counter(
    "pronaos_circuit_skipped_requests_total",
    "Provider attempts skipped because the breaker was OPEN. "
    "This measures the *value* of the breaker — upstream calls saved.",
    labelnames=("provider",),
    registry=REGISTRY,
)


# --------------------------------------------------------------------------- #
# Streaming-cancellation metric (Phase 18)                                    #
# --------------------------------------------------------------------------- #
#
# A "cancelled" stream is one the client tore down before the upstream
# provider finished. Each tick represents one real-world cost-saving
# opportunity (the upstream connection was closed mid-response). Useful for
# capacity planning ("what fraction of our streams are cancelled?") and
# for alerting on a spike that suggests a downstream client bug.

streams_cancelled_total = Counter(
    "pronaos_streams_cancelled_total",
    "Streaming responses cancelled by the client mid-stream. Measured at "
    "the gateway's outbound generator; counts one per cancellation event.",
    labelnames=("provider", "model"),
    registry=REGISTRY,
)


# --------------------------------------------------------------------------- #
# Pre-flight quota gate (Phase 20)                                            #
# --------------------------------------------------------------------------- #
#
# Pre-flight denials are quota rejections issued BEFORE the upstream provider
# call — based on a heuristic token estimate vs the team's remaining budget.
# They save real money (a denied-anyway request never hits Groq/Anthropic).
# Distinguishing pre- vs post-flight denials in the counter lets dashboards
# answer "how many upstream calls did the preflight gate save?"

preflight_denials_total = Counter(
    "pronaos_preflight_denials_total",
    "Requests denied before the upstream call because the estimated total "
    "tokens (prompt + max_completion) exceeded the team's remaining budget. "
    "``reason`` is monthly_token_budget_exhausted or "
    "monthly_cost_budget_exhausted, matching the post-flight denial labels.",
    labelnames=("reason",),
    registry=REGISTRY,
)


# --------------------------------------------------------------------------- #
# Cost-aware routing (Phase 21)                                               #
# --------------------------------------------------------------------------- #
# Tracks every ``model="auto"`` resolution: which strategy picked which
# concrete model. ``selected_model`` is the full ``provider/model`` form
# so dashboards can answer "what does cheapest pick most often?" Cardinality
# is bounded by the catalog * strategies (~3 * ~25 = 75 series tops).

routing_decisions_total = Counter(
    "pronaos_routing_decisions_total",
    "Auto-routing decisions made when a client sent model='auto'. "
    "``strategy`` is the team's routing_strategy; ``selected_model`` is "
    "the concrete provider/model the scorer picked.",
    labelnames=("strategy", "selected_model"),
    registry=REGISTRY,
)


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def record_provider_success(
    provider: str,
    model: str,
    duration_seconds: float,
    prompt_tokens: int,
    completion_tokens: int,
    cost_hcents: int,
) -> None:
    """Single funnel for the provider counters so the call sites stay readable.

    Keeping this here (rather than inlining four `.labels(...).inc(...)` calls
    in chat.py) means a future re-shape of the labels only changes one file.
    """
    provider_requests_total.labels(provider=provider, model=model, status="success").inc()
    provider_request_duration_seconds.labels(provider=provider, model=model).observe(
        duration_seconds
    )
    if prompt_tokens > 0:
        provider_tokens_total.labels(provider=provider, model=model, direction="prompt").inc(
            prompt_tokens
        )
    if completion_tokens > 0:
        provider_tokens_total.labels(
            provider=provider, model=model, direction="completion"
        ).inc(completion_tokens)
    if cost_hcents > 0:
        provider_cost_hcents_total.labels(provider=provider, model=model).inc(cost_hcents)


def record_provider_error(provider: str, model: str) -> None:
    provider_requests_total.labels(provider=provider, model=model, status="error").inc()


def record_quota_denial(reason: str) -> None:
    quota_denials_total.labels(reason=reason).inc()


def record_cache_lookup(*, tier: str, result: str) -> None:
    """Record one cache decision. ``tier`` ∈ {exact, semantic}; ``result`` ∈
    {hit, miss, skip}. ``skip`` is its own category so the hit-rate panel
    can compute ``hits / (hits + miss)`` and ignore skip — otherwise
    streaming-heavy traffic would tank the apparent hit rate even when
    the cache is doing its job."""
    cache_lookups_total.labels(tier=tier, result=result).inc()


def record_guardrail_hit(*, rule: str, action: str, direction: str) -> None:
    """Increment the guardrail counter for one rule firing.

    Called once per RuleHit (so multiple emails in one prompt produce
    multiple counter ticks). That's the right granularity for the "PII
    redactions per minute" panel — it matches how dashboards count
    things you'd talk about as "events"."""
    guardrail_hits_total.labels(rule=rule, action=action, direction=direction).inc()


# String→Prometheus-value mapping for the circuit_state gauge. Stable
# numeric encoding so PromQL queries are stable and a Grafana threshold
# panel can colour CLOSED/HALF_OPEN/OPEN consistently.
_CIRCUIT_STATE_VALUES: dict[str, float] = {
    "closed": 0.0,
    "half_open": 1.0,
    "open": 2.0,
}


def record_circuit_state(provider: str, state: str) -> None:
    """Set the circuit-state gauge for ``provider`` from the breaker's
    state string. Called by the registry-snapshot exporter — see
    ``observability/exporter.py`` for the scheduling logic."""
    value = _CIRCUIT_STATE_VALUES.get(state)
    if value is None:
        # Unknown state — refuse silently rather than emit a misleading
        # numeric value that PromQL would mis-colour.
        return
    circuit_state.labels(provider=provider).set(value)


def record_circuit_trip(provider: str) -> None:
    """Bump the trip counter. Called by the failover layer when a
    provider call fails AND the breaker transitions to OPEN — i.e.
    exactly once per trip event."""
    circuit_trips_total.labels(provider=provider).inc()


def record_circuit_skipped(provider: str) -> None:
    """Bump the skipped-requests counter. Called by failover when the
    breaker for ``provider`` was OPEN at request time — measures
    upstream calls the breaker actively saved."""
    circuit_skipped_requests_total.labels(provider=provider).inc()


def record_stream_cancelled(provider: str, model: str) -> None:
    """Bump the streaming-cancellation counter. Called by the chat
    handler's streaming generator when ``CancelledError`` fires —
    i.e. the client closed the connection before the response was
    fully streamed. One tick per cancellation event."""
    streams_cancelled_total.labels(provider=provider, model=model).inc()


def record_preflight_denial(reason: str) -> None:
    """Bump the preflight-denial counter. Called by the chat handler
    when the token estimator + budget check decides this request
    cannot succeed and rejects it before the upstream call.
    ``reason`` mirrors the post-flight denial reasons so dashboards
    can sum across both layers."""
    preflight_denials_total.labels(reason=reason).inc()


def record_routing_decision(*, strategy: str, selected_model: str) -> None:
    """Bump the auto-routing decision counter. Called by the chat
    handler when ``model="auto"`` resolves to a concrete provider/model
    via the cost-aware scorer."""
    routing_decisions_total.labels(
        strategy=strategy, selected_model=selected_model
    ).inc()


__all__ = [
    "REGISTRY",
    "cache_lookups_total",
    "circuit_skipped_requests_total",
    "circuit_state",
    "circuit_trips_total",
    "guardrail_hits_total",
    "http_request_duration_seconds",
    "http_requests_total",
    "preflight_denials_total",
    "provider_cost_hcents_total",
    "provider_request_duration_seconds",
    "provider_requests_total",
    "provider_tokens_total",
    "quota_denials_total",
    "record_cache_lookup",
    "record_circuit_skipped",
    "record_circuit_state",
    "record_circuit_trip",
    "record_guardrail_hit",
    "record_preflight_denial",
    "record_provider_error",
    "record_provider_success",
    "record_quota_denial",
    "record_routing_decision",
    "record_stream_cancelled",
    "routing_decisions_total",
    "streams_cancelled_total",
]
