# Pronaos — Architecture

This document describes the high-level architecture of Pronaos. For focused deep-dives see `docs/architecture/`.

## Goals

1. **Single governed hop** for every LLM call an organization makes.
2. **Latency-neutral** — the gateway must add < 10 ms p50 overhead over a direct provider call.
3. **Provider-agnostic** — same API surface regardless of upstream.
4. **Observable by default** — no opt-in required for traces, metrics, or audit.
5. **Safe by default** — guardrails and quotas run before egress, not after.
6. **Operable** — runbooks, chaos-tested failover, and horizontal scaling as first-class concerns.

## Non-goals

- Fine-tuning orchestration (that's a separate product).
- Being a prompt IDE (pair with existing tools).
- Being an agent framework (the gateway is consumed *by* agent frameworks).

## Request flow

```
┌────────────┐     ┌────────────────────────────────────────────────────────────┐
│ client app │───► │                     Pronaos                          │
└────────────┘     │                                                            │
                   │  auth ─► quota ─► policy ─► cache ─► router ─► provider   │
                   │                                                    │       │
                   │                                              response      │
                   │                                                    │       │
                   │         egress guardrails ─► audit ─► observability ◄──────│
                   └────────────────────────────────────────────────────────────┘
                          │                  │                 │
                          ▼                  ▼                 ▼
                     Postgres          Redis + Qdrant      OTEL collector
                 (tenants, keys,     (rate limits,        (traces, metrics,
                  audit, quotas)      cache layers)        structured logs)
```

### 1. Auth
- API-key based for server-to-server calls
- OIDC/SSO (Keycloak, Auth0, Azure AD) for human and admin access
- Every request resolves to a `(tenant_id, team_id, principal_id)` tuple

### 2. Quota enforcement
- Token-bucket and sliding-window limiters in Redis
- Enforced at tenant, team, and key scope
- Soft limit triggers a header warning; hard limit returns `429`

### 3. Policy & guardrails (ingress)
- PII redaction (regex + Presidio when available)
- Prompt-injection classifier (small model + rule engine)
- Tenant-scoped policies: allowed providers, allowed models, max tokens, tool-use restrictions

### 4. Cache lookup (shipped — Phase 7)
- **L1 (exact, Redis)**: SHA-256 hash of `(messages, temperature, max_tokens)`
  under `cache:exact:{tenant_id}:{model}:{digest}`. Sub-millisecond hits.
- **L2 (semantic, Qdrant)**: `sentence-transformers/all-MiniLM-L6-v2` embedding
  of the latest user message; cosine similarity ≥ threshold (default 0.95)
  under a `tenant_id` + `model` payload filter.
- **Read path:** L1 → L2 → provider, with promotion-on-L2-hit into L1.
- **Write path:** L1 + L2 concurrent dual-write after a successful response.
- **Bypass:** `stream=true` or `temperature>0` always skip the cache.
- Tenant isolation is enforced by construction (key path / payload filter),
  not by runtime check — no API shape derives another tenant's lookup.
- Every backend fails open: cache outage degrades to direct provider call.

### 5. Router
- Primary/fallback chain defined per tenant/route
- Latency and error budgets drive automatic reshuffling
- Circuit breaker per provider/model

### 6. Provider call
- Streaming responses passed through with backpressure
- Token usage captured for cost accounting
- Retries with jitter on retryable errors only

### 7. Egress guardrails
- Optional content policies (PII leak-back, toxicity, banned outputs)
- Can strip, replace, or block

### 8. Cost accounting, audit & observability
- **Cost accounting (shipped)**: every successful chat call writes one row to
  `usage_records` (provider, model, tokens, cost in hundredths-of-a-cent,
  status, request_id). Queryable through `GET /v1/admin/usage` with filters
  (time range, team, provider, model, status) returning paginated rows and
  aggregate totals over the same WHERE clause. Tenant-isolated by default.
  `pronaos-cli team chargeback` consumes the same data for offline reports.
- **Append-only hash-chained audit log (roadmap)**: a separate, tamper-evident
  record of full request/response hashes for compliance — distinct from the
  cost-accounting table, which captures aggregates only.
- OTEL span emitted for every stage; trace id returned in response header.
- Prometheus counters/histograms for quotas, cache hit rate, provider latency.

## Data model (sketch)

```
tenants         (id, name, created_at, plan)
teams           (id, tenant_id, name, monthly_token_budget, current_period_tokens, period_resets_at)
api_keys        (id, team_id, prefix, key_hash, scopes, rps_limit, revoked_at)
usage_records   (id, ts, tenant_id, team_id, key_id, provider, model, prompt_tokens,
                 completion_tokens, cost_hcents, request_id, status)
audit_log       (id, ts, tenant_id, actor, request_hash, response_hash, prev_hash)         -- roadmap
provider_routes (tenant_id, route, primary, fallbacks[], policy)                           -- roadmap
eval_runs       (id, suite, prompt_version, model, score, created_at)                      -- roadmap
```

`usage_records` is the **per-call audit table** powering chargeback and FinOps queries.
It is intentionally **not foreign-keyed** to tenants/teams/keys — when a tenant is
deleted we still want their historical spend preserved for compliance and finance.
Indexed `(tenant_id, ts)` and `(team_id, ts)` for the two hottest query shapes.

## Deployment shape

- One stateless `pronaos` container, horizontally scalable behind a load balancer
- Postgres for persistence (RDS Aurora in production)
- Redis for caches and rate limits (ElastiCache)
- Qdrant for semantic cache (self-hosted or managed)
- OTEL collector + Grafana Cloud or self-hosted Tempo/Prometheus/Loki
- Helm chart in `deploy/helm/pronaos/`

## Failure modes & mitigations

| Failure                       | Mitigation                                                    |
|-------------------------------|---------------------------------------------------------------|
| Provider outage               | Fallback chain; circuit breaker; cached responses when safe   |
| Redis unavailable             | Fail-open on caches (add p95 latency); fail-closed on quotas |
| Postgres down                 | Short in-memory audit buffer + async replay on recovery      |
| Embedding model unavailable   | Semantic cache disabled; exact cache still works             |
| OTEL collector unreachable    | Drop spans (batched), never block request path               |
| PII/guardrail classifier slow | Budgeted with hard timeout; fail-closed on configured routes |
