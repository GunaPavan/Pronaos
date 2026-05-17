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

from prometheus_client import CollectorRegistry, Counter, Histogram

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


__all__ = [
    "REGISTRY",
    "http_requests_total",
    "http_request_duration_seconds",
    "provider_requests_total",
    "provider_request_duration_seconds",
    "provider_tokens_total",
    "provider_cost_hcents_total",
    "quota_denials_total",
    "cache_lookups_total",
    "record_provider_success",
    "record_provider_error",
    "record_quota_denial",
    "record_cache_lookup",
]
