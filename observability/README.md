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

| Metric                                       | Type      | Labels                       |
| -------------------------------------------- | --------- | ---------------------------- |
| `pronaos_http_requests_total`                | counter   | method, route, status_code   |
| `pronaos_http_request_duration_seconds`      | histogram | method, route                |
| `pronaos_provider_requests_total`            | counter   | provider, model, status      |
| `pronaos_provider_request_duration_seconds`  | histogram | provider, model              |
| `pronaos_provider_tokens_total`              | counter   | provider, model, direction   |
| `pronaos_provider_cost_hcents_total`         | counter   | provider, model              |
| `pronaos_quota_denials_total`                | counter   | reason                       |
| `pronaos_preflight_denials_total`            | counter   | reason                       |
| `pronaos_cache_lookups_total`                | counter   | tier, result                 |
| `pronaos_guardrail_hits_total`               | counter   | rule, action, direction      |
| `pronaos_circuit_state`                      | gauge     | provider                     |
| `pronaos_circuit_trips_total`                | counter   | provider                     |
| `pronaos_circuit_skipped_requests_total`     | counter   | provider                     |
| `pronaos_streams_cancelled_total`            | counter   | provider, model              |

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

## Guardrails (Phase 8)

Ingress + egress content inspection inside the chat handler:

- **Ingress** (before cache lookup): scans each `user` message. PII gets
  redacted; the cache key is derived from the **post-redaction** text so
  two requests differing only in identifiers collide on the same cache
  entry — the cache layer never sees the raw PII.
- **Egress** (after provider call, before cache write): scans the
  assistant response. Catches the model regurgitating training-set PII.
  Can only REDACT — by the time we're here the upstream call already
  happened.

**Rules that ship enabled by default:**

| Rule name | What it catches | Default action |
| --- | --- | --- |
| `pii.email` | standard `local@domain` shape | REDACT |
| `pii.phone` | US-style phone numbers (with common separators) | REDACT |
| `pii.ssn` | hyphenated `NNN-NN-NNNN` | REDACT |
| `pii.credit_card` | 13–19 digits with Luhn-checksum filter | REDACT |
| `pii.ipv4` | dotted-quad IPv4 | REDACT |
| `injection` | known jailbreak preambles ("ignore previous instructions", etc.) | LOG_ONLY |

The `injection` rule defaults to LOG_ONLY because legitimate prompts
about prompt engineering / red-teaming / safety research would
otherwise trip it. Operators tightening enforcement can flip it to
BLOCK via the per-tenant policy override (Phase 8.2):

```bash
pronaos-cli team set-guardrail-policy <team-id> --set-action injection:block
```

The same column lets ops disable specific rules when redaction
degrades quality on topically-relevant content — see Empirical
claim #3 in the top-level README for the IPv4-in-networking-prompt
case study.

**Response headers** the gateway stamps:

- `X-Pronaos-Guardrails: redacted:<rule1>,<rule2>` — one or more
  redactions were applied; request still went through
- `X-Pronaos-Guardrails: blocked:<rule>` — request short-circuited
  with 422
- absent — no rule fired (or only LOG_ONLY hits)

**Tuning knobs (env vars):**

| Variable | Default | What it does |
| --- | --- | --- |
| `PRONAOS_GUARDRAILS_ENABLED` | `true` | Set `false` to skip all guardrail scanning (e.g. for interactive prompt-engineering work where false-positives are annoying). |

## Circuit breaker (Phase 15)

Per-provider three-state breaker on the failover path:

- **CLOSED** — calls allowed; failures tracked
- **OPEN** — calls denied; the provider is skipped entirely (saves the
  upstream connect timeout). Transitions to HALF_OPEN after the
  recovery window.
- **HALF_OPEN** — one probe allowed; success → CLOSED, failure → OPEN
  with a fresh timer

**Defaults:** trip after 5 consecutive retryable failures, 30 s recovery
window. Auth errors (4xx with a credential reason) deliberately do
NOT trip the breaker — a misconfigured key isn't a provider-health
signal, and locking out a provider whose key the operator is fixing
would be the wrong move.

**Three metrics:**

- `pronaos_circuit_state{provider}` — gauge (0=closed, 1=half_open,
  2=open). The /metrics handler refreshes this from the registry on
  every scrape so OPEN→HALF_OPEN timer transitions are visible
  without requiring a request to fire.
- `pronaos_circuit_trips_total{provider}` — discrete trip events
  (CLOSED/HALF_OPEN → OPEN). A long outage adds 1, not many.
- `pronaos_circuit_skipped_requests_total{provider}` — upstream calls
  the breaker actively saved (incremented when failover sees OPEN
  and skips the provider entirely)

**Two new Grafana panels** on the Overview dashboard:

- *Circuit breaker state by provider* — colour-coded stat panel
  (green = CLOSED, yellow = HALF_OPEN, red = OPEN) per provider
- *Circuit trips + skipped requests (5m windows)* — timeseries
  showing breaker activity over time

Live demo (~26× speedup on streaming under provider degradation) in
the top-level README, Empirical claim #6.

## Streaming cancellation (Phase 18)

When the client disconnects mid-stream — typical with `curl --max-time`,
browser tab close, mobile network drop — the gateway:

1. Catches `asyncio.CancelledError` (which is a `BaseException`
   subclass, so the previous `except Exception` block silently
   dropped every cancellation event — bug caught + fixed)
2. Closes the upstream provider connection via httpx's
   `async with stream(...)` context exit — *this* is the real cost
   saving (no more provider tokens generated)
3. Ticks `pronaos_streams_cancelled_total{provider, model}`
4. Logs a structured `stream.cancelled` event with
   `partial_completion_tokens` so operators can see how much
   upstream work was consumed before the disconnect
5. Re-raises CancelledError so Starlette's response runner can do
   its own cleanup

**One documented limitation:** DB-level bookkeeping (audit row,
usage_record) on the cancel path is best-effort — aiosqlite tears
down the connection during cancellation cleanup, so a fresh-session
write may not survive. The metric + log are the production
observability commitment.

## Pre-flight quota gate (Phase 20)

Token-budget enforcement runs *twice*:

- **Pre-flight** (this section): estimate the request's token cost
  before the upstream call. If the team can't afford it, deny up
  front. Saves the upstream call cost on requests that would deny
  post-flight anyway.
- **Post-flight** (existing): record actual tokens consumed and
  enforce the budget on subsequent requests.

The estimator is a heuristic (`words × 1.30 + punctuation` for
Latin scripts; `chars / 2.5` for CJK; 4-token per-message overhead
matching OpenAI's wrapping) — calibrated within ±15% of Groq's
actual prompt_tokens count on representative English samples.
Defaults to `max_tokens = 4096` when the caller didn't set one
(conservative fail-safe — better to over-estimate and force
unbounded requests through pre-flight rejection than under-estimate
and let a runaway slip past).

**Metric:** `pronaos_preflight_denials_total{reason}` — labels
match the post-flight `pronaos_quota_denials_total{reason}` set so
dashboards can sum across both layers ("total denials, by reason")
or split them ("how many upstream calls did the preflight gate
save?").

**Response header on a preflight denial:**

- `X-Pronaos-Preflight-Estimate: <int>` — lets clients distinguish
  preflight from post-flight denial and decide whether to retry
  with smaller `max_tokens`

Live demo with cost-savings claim in the top-level README,
Empirical claim #7.

## Webhooks (Phase 19)

Operational events push out as HMAC-signed POSTs to a tenant-
configured receiver. Three event types fire today:

| Event | Trigger |
| --- | --- |
| `quota.exhausted` | Rate-limit or budget denial in `enforce_quotas` |
| `circuit.tripped` | A provider's circuit breaker transitioned to OPEN |
| `audit.chain_broken` | `audit verify` detected hash-mismatch / prev-mismatch |

**Signature scheme** (matches the GitHub-webhook convention so
existing receiver libraries Just Work):

```
X-Pronaos-Signature: sha256=<hex of HMAC_SHA256(secret, body)>
X-Pronaos-Event:     <event-name>
X-Pronaos-Delivery:  <uuid per delivery attempt>
```

**Payload schema:**

```json
{
  "event": "quota.exhausted",
  "ts": 1779087920.316899,
  "tenant_id": "e8e1d4194db14048821fc94ae2a24f8f",
  "data": {
    "team_id": "...",
    "team_name": "engineering",
    "reason": "monthly_token_budget_exhausted",
    "retry_after_seconds": 1184079
  }
}
```

**Retry policy:** up to 3 attempts on 5xx and connection errors with
exponential backoff (0.5 s → 1 s → 2 s). 4xx → no retry (the
receiver said our payload was bad; retrying won't help).

**Configure with the CLI or admin API:**

```bash
pronaos-cli tenant set-webhook <tenant-id> \
    --url https://hooks.slack.com/services/X/Y/Z \
    --secret <shared-secret>

pronaos-cli tenant set-webhook <tenant-id> --show     # secret is redacted
pronaos-cli tenant set-webhook <tenant-id> --clear    # disable
```

A reference receiver lives at `scripts/webhook_receiver.py` — point
the tenant's webhook URL at it to see signed deliveries in your
terminal during a demo.

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
