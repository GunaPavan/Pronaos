# Roadmap — 12 weeks to production-credible

This is a realistic, opinionated 12-week build plan. Each milestone ends with a visible, demo-able artifact — not just code. The goal is a repo that reads like a product at week 12.

## Phase 1 — Core proxy (weeks 1–2)

**Goal:** any OpenAI SDK can point at the gateway and get a streamed completion back from real providers.

- [ ] FastAPI skeleton with OpenAI-compatible `/v1/chat/completions`
- [ ] Provider interface + adapters: OpenAI, Anthropic, Bedrock
- [ ] SSE streaming passthrough with backpressure
- [ ] API-key auth (hashed, scoped, revocable)
- [ ] Structured logging (structlog) + request correlation id
- [ ] Smoke tests in CI

**Demo:** `openai.ChatCompletion.create(base_url="http://localhost:8080/v1", ...)` returns a streamed response routed to Anthropic.

## Phase 2 — Control plane (weeks 3–4)

**Goal:** an admin can create tenants, teams, and keys, and see usage.

- [ ] Postgres schema + Alembic migrations
- [ ] Admin API: tenants, teams, keys, quotas
- [ ] Per-key + per-team quotas (token and cost), Redis token bucket
- [ ] Cost calculation table (per model, per provider, per token)
- [ ] `GET /v1/usage` report endpoint

**Demo:** `pronaos-cli tenant create`, `pronaos-cli key issue --team eng`, usage report JSON.

## Phase 3 — Observability (weeks 5–6)

**Goal:** every call is fully traceable; a dashboard visualizes spend and latency.

- [ ] OpenTelemetry spans across auth → cache → provider → guardrail
- [ ] Prometheus metrics: RPS, latency, error rate, cache hit rate, cost/sec
- [ ] Grafana dashboards committed under `observability/grafana/`
- [ ] Audit log writer with hash-chain
- [ ] Trace id surfaced in `X-Pronaos-Trace-Id` response header

**Demo:** Grafana dashboard screenshot in README + walkthrough of a single trace in Tempo.

## Phase 4 — Resilience (weeks 7–8)

**Goal:** the gateway survives real provider outages.

- [ ] Semantic cache (Qdrant) with tenant-scoped collections
- [ ] Circuit breaker per provider/model
- [ ] Automatic fallback chain driven by latency + error budget
- [ ] Chaos tests that kill providers mid-stream and assert no 5xx to client

**Demo:** chaos test video: OpenAI killed → Anthropic takes over seamlessly → client sees no error.

## Phase 5 — Safety (weeks 9–10)

**Goal:** the gateway will not leak PII or accept adversarial prompts.

- [ ] PII detection and redaction (ingress and egress)
- [ ] Prompt-injection classifier (lightweight + LLM-as-judge fallback)
- [ ] Tenant-scoped policy engine (allowed providers, models, max tokens, forbidden tools)
- [ ] Red-team test suite in CI

**Demo:** attempt injection attacks on the public demo endpoint — all blocked, all logged.

## Phase 6 — Product surface (weeks 11–12)

**Goal:** the repo looks like a product a company could adopt today.

- [ ] Evaluation harness with LLM-as-judge and golden-set regression tests
- [ ] Next.js admin UI (tenants, keys, usage, traces)
- [ ] Helm chart + Terraform AWS module
- [ ] Python + TypeScript SDK packages
- [ ] Load test report (k6) — 1000 RPS sustained, p95 latency tracked
- [ ] Public demo endpoint + live dashboard
- [ ] Architecture diagram video walkthrough

**Demo:** end-to-end recording: deploy via Helm → issue key → run agent app against gateway → watch trace/cost/quota update in UI.

---

## Success metrics at week 12

- 1000+ RPS sustained in load test, p50 < 30 ms overhead
- 99.95% test coverage on core proxy path; 70%+ overall
- Provider outage recovery < 200 ms (proven in chaos tests)
- Semantic cache hit rate > 30% on representative workload
- All dashboards provisioned-as-code (no clicking in Grafana)
- One deploy command: `helm install pronaos deploy/helm/pronaos`
- Public demo URL + 2-minute recruiter-friendly walkthrough video linked from README
