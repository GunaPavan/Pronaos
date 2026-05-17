# Observability stack

`docker compose up -d` brings up Prometheus, Grafana, Tempo, and an OTEL
collector wired together. Once Pronaos is running on the host (`tasks dev`),
the full pipeline produces metrics and traces with no extra configuration.

## What you get

| Service          | URL                       | Purpose                                              |
| ---------------- | ------------------------- | ---------------------------------------------------- |
| Pronaos          | `http://localhost:8080`   | The gateway itself                                   |
| Pronaos /metrics | `http://localhost:8080/metrics` | Prometheus-format counters and histograms      |
| Grafana          | `http://localhost:3000`   | Dashboards (anonymous viewer enabled)                |
| Prometheus       | `http://localhost:9090`   | Raw query UI                                         |
| Tempo            | `http://localhost:3200`   | Trace storage (accessed via Grafana, not directly)   |
| OTEL collector   | `:4317` (gRPC) / `:4318` (HTTP) | Receives spans + metrics from the gateway      |

## Two dashboards ship out of the box

Open Grafana → **Dashboards → Pronaos** folder.

### `Pronaos — Overview`
Designed for incident response. At a glance:
- Current RPS, error rate, provider call rate, quota denials/s
- HTTP latency p50 / p95 / p99 (5-min rate)
- Provider RPS stacked by provider
- Quota denials by reason (rate-limit vs token-budget vs cost-budget)
- Provider latency p95 by upstream

### `Pronaos — FinOps`
Designed for the weekly cost review. At a glance:
- Total spend in the selected range (USD)
- Projected daily burn extrapolated from the last 5 min
- Tokens consumed in the range
- Spend-per-minute stacked by provider
- Tokens/sec by provider × direction (prompt vs completion)
- **Cache hit rate** (last 5m) and **cache lookups by outcome** (timeseries)
- Spend by model — sortable table over the whole range

> Cost numbers come from `pronaos_provider_cost_hcents_total`, which divides
> by **10,000** to render USD. The same per-call breakdown lives in the
> `usage_records` DB table — query that for tenant/team chargeback, or run
> `pronaos-cli team chargeback <team-id>`.

## Metrics reference

All metrics are prefixed `pronaos_` and exposed on `:8080/metrics`.

| Metric                                     | Type      | Labels                       |
| ------------------------------------------ | --------- | ---------------------------- |
| `pronaos_http_requests_total`              | counter   | method, route, status_code   |
| `pronaos_http_request_duration_seconds`    | histogram | method, route                |
| `pronaos_provider_requests_total`          | counter   | provider, model, status      |
| `pronaos_provider_request_duration_seconds`| histogram | provider, model              |
| `pronaos_provider_tokens_total`            | counter   | provider, model, direction   |
| `pronaos_provider_cost_hcents_total`       | counter   | provider, model              |
| `pronaos_quota_denials_total`              | counter   | reason                       |
| `pronaos_cache_lookups_total`              | counter   | tier, result                 |

### A note on cardinality

`tenant_id` and `team_id` are **deliberately not** Prometheus labels — they'd
explode series count for any deployment with many customers. Per-tenant
cost queries live in the `usage_records` Postgres table (queryable via
`GET /v1/admin/usage` or the chargeback CLI). The Prometheus surface is for
operational, low-cardinality views.

## Semantic cache (Phase 7)

Two-tier response cache between auth and provider:

- **L1 (exact)** — Redis. Canonical SHA-256 hash of
  `(messages, temperature, max_tokens)` under
  `cache:exact:{tenant_id}:{model}:{digest}`. Hit = sub-millisecond.
- **L2 (semantic)** — Qdrant. Embeds the latest user message with
  `sentence-transformers/all-MiniLM-L6-v2` (384-dim, local CPU) and
  retrieves under a `tenant_id` + `model` payload filter at cosine
  similarity ≥ `PRONAOS_SEMANTIC_CACHE_THRESHOLD` (default `0.95`).
  Hit = ~10 ms (embedding) + ~5 ms (vector lookup).

**Read path:** L1 → L2 → provider. On L2 hit the response is also
promoted into L1 so the same paraphrase hits the cheaper path next time.
**Write path:** dual-write to L1 + L2 concurrently after a successful
provider response.

**Bypassed automatically when:** `stream=true` (caching streaming defeats
the point), `temperature > 0` (caller asked for variety). Both increment
`pronaos_cache_lookups_total{result="skip"}` so the dashboard hit-rate
math (`hits / (hits + miss)`) stays honest.

**Tenant isolation:** the `tenant_id` is a literal path segment in the L1
key and a payload filter in L2. There is no API shape that lets one
tenant's request derive another tenant's lookup — enforced by
construction, not runtime check.

**Tuning knobs (env vars):**

| Variable | Default | What it does |
| --- | --- | --- |
| `PRONAOS_REDIS_URL` | unset | Set to `redis://localhost:6379/0` to enable L1. Unset = cache disabled. |
| `PRONAOS_SEMANTIC_CACHE_ENABLED` | `false` | Set `true` to enable L2. Costs ~1-2 s startup (PyTorch boot) + ~250 MB RAM. |
| `PRONAOS_QDRANT_URL` | `http://localhost:6333` | Qdrant endpoint. |
| `PRONAOS_SEMANTIC_CACHE_THRESHOLD` | `0.95` | Cosine similarity floor for L2 hits. Lower = more hits + more false-positive risk. |

## OTEL spans

Beyond the auto-instrumented FastAPI / httpx / SQLAlchemy spans, the
gateway emits two named spans:

- **`pronaos.quota.check`** — attributes:
  `pronaos.quota.allowed`, `pronaos.quota.reason` (on denial only)
- **`pronaos.provider.call`** — attributes:
  `pronaos.provider`, `pronaos.model`, `pronaos.prompt_tokens`,
  `pronaos.completion_tokens`, `pronaos.cost_hcents`,
  `pronaos.duration_seconds`

Use these to pivot in trace exploration — for example, "show me traces in
the last hour where `pronaos.cost_hcents > 5000` and the parent span took
over 2 seconds."

## Local development tips

- Grafana persists dashboards across runs in the `pronaos-grafana` Docker
  volume. To start clean: `docker compose down -v`.
- Prometheus scrapes `host.docker.internal:8080` by default — set
  `PRONAOS_HOST=0.0.0.0` in `.env` if you've bound Pronaos to a specific
  interface.
- Tempo retains traces for 1 hour by default; tune in
  `observability/tempo/config.yaml` if you want longer.
