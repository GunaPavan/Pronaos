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

### 4. Cache lookup
- Exact-match Redis cache keyed on `(tenant, model, normalized_prompt)`
- Semantic cache via Qdrant — embedding distance threshold per tenant
- Always tenant-isolated; no cross-tenant cache poisoning possible

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

### 8. Audit & observability
- Append-only hash-chained audit log (Postgres table + periodic S3 export)
- OTEL span emitted for every stage; trace id returned in response header
- Prometheus counters/histograms for quotas, cache hit rate, provider latency

## Data model (sketch)

```
tenants (id, name, created_at, plan)
teams (id, tenant_id, name)
api_keys (id, team_id, hashed_key, scopes, revoked_at)
quotas (tenant_id, scope, limit_tokens, limit_cost_cents, window)
audit_log (id, ts, tenant_id, actor, request_hash, response_hash, prev_hash)
provider_routes (tenant_id, route, primary, fallbacks[], policy)
eval_runs (id, suite, prompt_version, model, score, created_at)
```

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
