# Pronaos — Architecture

High-level architecture of what's built and running today. For deeper
operational detail see [`observability/README.md`](observability/README.md);
for empirical claims about behaviour see the top-level [`README.md`](README.md).

## Goals

1. **Single governed hop** for every LLM call an organization makes.
2. **Provider-agnostic** — same OpenAI-shape API regardless of upstream
   (12 providers wired today; bidirectional translation for Anthropic).
3. **Observable by default** — every request emits Prometheus metrics
   and OTEL spans; no opt-in.
4. **Safe by default** — guardrails, quotas, and the model allowlist
   run before the upstream call.
5. **Operable** — circuit breaker, webhook event delivery, and hash-
   chained audit log as first-class concerns.

## Non-goals

- Fine-tuning orchestration.
- A prompt-authoring IDE.
- An agent framework — the gateway is consumed *by* agent frameworks.

## Request flow

```
┌────────────┐     ┌──────────────────────────────────────────────────────────────┐
│ client app │───► │                         Pronaos                              │
└────────────┘     │                                                              │
                   │  auth ─► allowlist ─► preflight ─► guardrails-in ─►          │
                   │  cache lookup ─► failover (circuit-breaker-aware) ─►         │
                   │  provider ─► guardrails-out ─► cache write ─►                │
                   │  audit append ─► usage record ─► metrics + spans             │
                   │                                                              │
                   └──────────────────────────────────────────────────────────────┘
                          │                  │                 │           │
                          ▼                  ▼                 ▼           ▼
                     Postgres          Redis + Qdrant      OTEL collector  Webhook
                 (tenants, teams,     (rate limits,        (traces,        dispatcher
                  keys, audit,         L1 + L2 cache)      metrics)       (Slack /
                  usage, policy,                                          PagerDuty /
                  webhooks)                                                custom)
```

### 1. Auth

- API-key based (argon2 hash + non-secret prefix); soft revocation
  (`revoked_at`).
- Every request resolves to a `Principal` carrying `tenant_id`,
  `team_id`, `key_id`, scopes, `rps_limit`, monthly token + cost
  budgets, guardrail policy, model allowlist, webhook config.
- Scopes today: `chat:write`, `admin:usage`.
- OIDC/SSO for human admin access is on the roadmap; not shipped.

### 2. Model allowlist gate

- Per-team `allowed_models` column (JSON list of fnmatch patterns).
- `NULL` = unrestricted (backwards-compat default); `[]` = explicit
  deny-all (paused team without revoking keys).
- Runs first inside the handler — before any expensive work — so a
  denied request never spends quota or hits a guardrail.
- Returns `403 model_not_allowed` with the offending model name.

### 3. Pre-flight token estimator + quota gate

- Heuristic estimator (words × 1.30 + punctuation; chars/2.5 for
  non-Latin scripts; per-message overhead = 4 tokens; defaults
  `max_tokens` to 4096 if caller omitted it).
- Calibrated within ±15% of Groq's actual tokenizer on representative
  English samples — a budget guardrail, not a billing oracle.
- `QuotaTracker.check_preflight(team_id, estimated_tokens)` rejects
  up-front (`429`) if the estimate would push the team over its
  monthly token budget. Saves the upstream call on requests that
  would deny post-flight anyway.
- The existing per-key RPS token-bucket + per-team token + per-team
  cost budgets all still enforce post-flight via `enforce_quotas`.
- Per-tenant rate limiter backend: in-memory (dev) or Redis Lua (prod).

### 4. Guardrails (ingress)

- Five PII detectors: `pii.email`, `pii.phone`, `pii.ssn`,
  `pii.credit_card` (Luhn-validated), `pii.ipv4`.
- One injection detector: regex/rule-based heuristic — not a classifier.
- Actions: `BLOCK` | `REDACT` | `LOG_ONLY`. Default actions documented
  in `observability/README.md`.
- Per-team `guardrail_policy` JSON overrides defaults at request time
  (disable a rule, change its action). Managed via
  `pronaos-cli team set-guardrail-policy` or the admin API.

### 5. Cache lookup (two-tier)

- **L1 exact (Redis)**: SHA-256 hash of `(messages, temperature,
  max_tokens)` under `cache:exact:{tenant_id}:{model}:{digest}`.
  Sub-millisecond hits.
- **L2 semantic (Qdrant)**: `sentence-transformers/all-MiniLM-L6-v2`
  embedding of the latest user message; cosine similarity ≥ threshold
  (default 0.95) under a `tenant_id` + `model` payload filter.
- **Read path:** L1 → L2 → provider, with promotion-on-L2-hit into L1.
- **Write path:** dual-write to L1 + L2 after a successful response.
- **Bypassed for:** `stream=true`, `temperature > 0`, and agent-loop
  turns (any message with `role:"tool"` or assistant `tool_calls`).
  All three bypass paths increment `pronaos_cache_lookups_total{result="skip"}`.
- Tenant isolation is enforced by construction (key path / payload
  filter), not by runtime check.
- Every backend fails open: cache outage degrades to a direct
  provider call.

### 6. Failover + circuit breaker

- Routing plan resolves the model prefix to a primary provider and
  zero or more fallbacks.
- Per-process per-provider circuit breaker on the failover path:
  CLOSED → OPEN after 5 consecutive retryable failures → HALF_OPEN
  after 30 s → CLOSED on successful probe.
- OPEN providers are skipped *before* any HTTP attempt — saves the
  connect-refused timeout (`pronaos_circuit_skipped_requests_total`).
- Auth errors deliberately don't trip the breaker — a misconfigured
  key isn't a provider-health signal.
- Trip events fire a `circuit.tripped` webhook to the tenant's
  configured receiver.

### 7. Provider call

- Streaming responses are passed through with backpressure; cancellation
  propagates cleanly to the upstream connection (httpx `async with
  stream(...)` close on `CancelledError`). Each cancellation ticks
  `pronaos_streams_cancelled_total`.
- Two native shapes: OpenAI-compat (one adapter, 11 providers) +
  Anthropic native (translates `tools`/`tool_choice`/`tool_use` ↔
  `tool_calls` in both directions, streaming + non-streaming).
- Token usage captured from the provider's `usage` block; cost
  computed from the per-model pricing in `providers/catalog.py`.

### 8. Guardrails (egress)

- Scans the assistant response for PII leak-back. Can only REDACT —
  by the time we're here the upstream call has happened.
- Runs on both streaming and non-streaming paths (the streaming
  variant scans the assembled content at stream close).
- Toxicity / banned-output detectors are not shipped.

### 9. Audit, usage, observability

- **Per-call usage row** (`usage_records`): tenant/team/key/provider/
  model, prompt+completion tokens, `cost_hcents` (hundredths of a
  cent for precise FinOps math), `request_id`, status. Powers
  `GET /v1/admin/usage` and `pronaos-cli team chargeback`.
- **Hash-chained audit log** (`audit_records`): per-tenant chain,
  `this_hash = sha256(prev_hash || canonical(request_hash,
  response_hash, ts, tenant_id, team_id, key_id, provider, model,
  request_id))`. Bodies are NOT stored — only hashes — so the
  audit log doesn't re-create the PII problem guardrails exist to
  solve. `pronaos-cli audit verify` walks the chain; chain-break
  events fire `audit.chain_broken` webhooks.
- **Prometheus** counters and histograms: HTTP, provider, quota,
  cache, guardrail, circuit, preflight, stream-cancellation — 14
  metric families total. Two pre-provisioned Grafana dashboards
  (Overview = 11 panels, FinOps = 8 panels).
- **OTEL spans**: auto-instrumented FastAPI / httpx / SQLAlchemy +
  named `pronaos.quota.check` and `pronaos.provider.call` spans
  with cost/token attributes for trace-time FinOps queries.

### 10. Outbound webhook events

- Tenant configures one webhook URL + shared secret. Events fire as
  HTTP POSTs with `X-Pronaos-Signature: sha256=<hex>` (HMAC-SHA256,
  GitHub-webhook-compatible scheme).
- Three event types: `quota.exhausted`, `circuit.tripped`,
  `audit.chain_broken`.
- Retry up to 3 attempts on 5xx + connection errors with exponential
  backoff (0.5 s → 1 s → 2 s). 4xx → no retry.
- Fire-and-forget asyncio task with a strong-reference set so the
  task isn't garbage-collected mid-flight.

## Data model (current)

```
tenants         (id, name, created_at, webhook_url, webhook_secret)
teams           (id, tenant_id, name, monthly_token_budget,
                 current_period_tokens, monthly_cost_hcents_budget,
                 current_period_cost_hcents, period_resets_at,
                 guardrail_policy, allowed_models)
api_keys        (id, team_id, prefix, key_hash, scopes, label,
                 created_at, revoked_at, last_used_at, rps_limit)
usage_records   (id, ts, tenant_id, team_id, key_id, provider, model,
                 prompt_tokens, completion_tokens, cost_hcents,
                 request_id, status)
audit_records   (id, ts, tenant_id, team_id, key_id, provider, model,
                 request_id, request_hash, response_hash,
                 prev_hash, this_hash)
```

`usage_records` and `audit_records` are intentionally **not**
foreign-keyed to tenants/teams/keys — when a tenant is deleted we
still want their historical spend and audit trail preserved for
compliance and finance. Indexed `(tenant_id, ts)` and `(team_id, ts)`
for the two hottest query shapes.

8 Alembic migrations apply cleanly from an empty DB (`0001` initial
auth schema → `0008` tenant webhook columns).

## Deployment shape

- One stateless `pronaos` container, horizontally scalable behind a
  load balancer.
- Postgres for persistence (SQLite in dev).
- Redis for L1 cache and (optionally) the rate limiter; Qdrant for
  the L2 semantic cache.
- OTEL collector + Prometheus / Tempo / Grafana for the observability
  pipeline. `docker compose up -d` brings up the full stack.

Helm chart + Terraform module are on the roadmap, not shipped.

## Failure modes & mitigations

| Failure                          | Mitigation                                                                                  |
|----------------------------------|---------------------------------------------------------------------------------------------|
| Single provider outage           | Failover chain skips known-bad providers (circuit breaker); fallback chain                  |
| Sustained provider outage        | Breaker OPEN saves the connect timeout on every subsequent request; webhook fires           |
| Redis unavailable                | Fail-open on caches (degrades to direct provider call); rate limiter degrades to in-memory  |
| Qdrant unavailable               | L2 disabled; L1 (Redis) and provider path still work                                        |
| Embedding model unavailable      | L2 disabled at startup; L1 + provider path unaffected                                       |
| OTEL collector unreachable       | Spans dropped (batched), never block the request path                                       |
| Client disconnect mid-stream     | Cancellation propagates to the upstream `httpx.stream` close; metric + log fire             |
| Audit chain tampered             | `pronaos-cli audit verify` exits non-zero; chain-break webhook fires on next CLI run        |

## What's not built

Items listed elsewhere as `🔜 roadmap` and the reason they're deferred:

- **Admin UI (Next.js)** — the CLI + admin API cover the operational
  surface; a UI is a polish step.
- **OIDC / SSO for human access** — server-to-server auth (the
  shipped path) is the hard-to-secure piece. Human SSO bolts on
  later via an identity provider.
- **Helm + Terraform** — `docker compose up -d` is the current
  one-command path; production-grade deploy is a packaging task.
- **Distributed circuit-breaker state** — per-process today; a
  Redis-backed registry would let multiple gateway replicas share
  trip decisions. Useful only at multi-replica scale.
- **Anthropic native streaming tool_use live verify** — implemented
  + unit-tested with realistic SSE bodies via `respx`. Needs a real
  Anthropic key for end-to-end demo against the actual API.
- **Multi-judge eval** — Anthropic + Groq concurrent grading for
  inter-judge agreement. Single-judge eval shipped; the gap is just
  averaging across judges.
- **Cost-aware routing** — pre-flight gate denies impossible requests,
  but auto-picking the cheapest eligible model that satisfies a
  request is a separate layer.
