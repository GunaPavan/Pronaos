# Pronaos

> Self-hosted LLM gateway. Unified OpenAI-compatible API for 12+ providers, multi-tenant auth, automatic failover, OpenTelemetry tracing.

Pronaos sits between your applications and every supported LLM provider, giving you one governed entry point for every AI call. Today it routes across **12+ providers** — Anthropic (native adapter) plus Groq, OpenAI, DeepSeek, Together, Fireworks, Perplexity, xAI, Cerebras, Mistral, OpenRouter, Azure OpenAI, and Ollama (local) via a single OpenAI-compatible adapter — behind one OpenAI-shape API, with multi-tenant authentication and automatic failover on retryable errors.

**The goal:** become the spine production AI teams need — unified API, per-tenant cost and quota management, semantic caching, PII redaction, prompt-injection defense, hash-chained audit logs, end-to-end OpenTelemetry, and CI-gated evaluation. Some of this ships today; the rest is on the [roadmap](ROADMAP.md).

---

## Why this exists

A 500-person company using LLMs today typically has:

- Multiple teams calling providers directly with ad-hoc API keys
- No unified view of spend, who is spending, or on what
- PII silently leaving the org in prompts
- A single-provider outage taking production down
- No defensible audit trail when legal or compliance ask what the AI said to a customer
- No way to A/B test model or prompt changes with real traffic

Pronaos is being built to collapse all of that into one governed hop.

---

## Feature highlights

| Area                     | Capability                                                                     | Status        |
| ------------------------ | ------------------------------------------------------------------------------ | ------------- |
| Universal API            | OpenAI-compatible `/v1/chat/completions` with streaming SSE                    | ✅ shipped    |
| Provider support         | 12+ providers via native Anthropic + generic OpenAI-compat adapter             | ✅ shipped    |
| Routing & failover       | Prefix-based provider selection; automatic retry across configured chain       | ✅ shipped    |
| Multi-tenancy            | Tenants, teams, scoped API keys with argon2 hashing                            | ✅ shipped    |
| Admin CLI                | Tenant / team / key lifecycle via `pronaos-cli`                                | ✅ shipped    |
| Structured logging       | `request_id` / `tenant_id` / `team_id` / `key_id` bound automatically          | ✅ shipped    |
| OpenTelemetry            | Instrumentation wired across FastAPI, httpx, SQLAlchemy                        | ✅ shipped    |
| Persistence              | SQLAlchemy + Alembic; SQLite (dev) and Postgres (prod) from the same code      | ✅ shipped    |
| Rate limits              | Per-key RPS token bucket — in-memory (dev) / Redis Lua (prod), zero-install dev | ✅ shipped    |
| Token budgets            | Per-team monthly token budget with calendar-month rollover, atomic SQL writes  | ✅ shipped    |
| Cost accounting & FinOps | Per-tenant chargeback, Grafana dashboards                                      | 🔜 roadmap    |
| Audit log                | Append-only, hash-chained, tamper-evident record of every call                 | 🔜 roadmap    |
| Semantic caching         | Embedding-based cache with tenant isolation and configurable TTLs              | 🔜 roadmap    |
| Guardrails               | PII redaction, prompt-injection defense, policy engine                         | 🔜 roadmap    |
| Evaluation harness       | Prompt / model regression tests, LLM-as-judge, CI-gated rollouts               | 🔜 roadmap    |
| Admin UI                 | Next.js dashboard for tenants, keys, usage, traces                             | 🔜 roadmap    |
| OIDC / SSO               | Keycloak / Auth0 / Azure AD for human and admin access                         | 🔜 roadmap    |
| Deploy                   | Helm chart + Terraform module for one-command production install               | 🔜 roadmap    |

---

## Architecture at a glance

See [`docs/architecture/overview.md`](docs/architecture/overview.md) for the full diagram and deep-dive. At a glance:

```
client app ─► Pronaos ─► [ router ─► guardrails ─► cache ─► provider SDK ]
                │                                                 │
                ├─► OTEL collector ─► Tempo / Prometheus / Loki   ├─► OpenAI
                ├─► Postgres (tenants, keys, quotas, audit)       ├─► Anthropic
                └─► Redis + vector store (cache, rate limits)     ├─► Bedrock / Gemini / …
```

Every request passes through: auth → quota check → policy/guardrail → cache lookup → routing → provider call → response guardrails → audit log → observability export.

---

## Quickstart (local development)

Prerequisites: Docker, Docker Compose, Python 3.12, `make`.

```bash
# 1. Clone and bootstrap
git clone <your-fork>
cd pronaos
cp .env.example .env

# 2. Bring up infra (postgres, redis, qdrant, otel collector, grafana, tempo)
make up

# 3. Install and run the gateway
make install
make dev

# 4. Open the docs
open http://localhost:8080/docs          # OpenAPI
open http://localhost:3000               # Grafana (admin / admin)
open http://localhost:3200               # Tempo traces
```

Run the smoke test:

```bash
curl -s http://localhost:8080/healthz | jq
```

---

## Project status

**In active development** — currently building out provider integrations and admin UI. See [`ROADMAP.md`](ROADMAP.md) for the full plan and [`PLAN.md`](PLAN.md) for the phase-by-phase execution checklist.

What works today:

- **Unified OpenAI-compatible API** (`/v1/chat/completions`, streaming SSE)
- **12+ provider routing** via one generic OpenAI-compat adapter + native Anthropic adapter: Anthropic, OpenAI, Groq, DeepSeek, Together, Fireworks, Perplexity, xAI, Cerebras, Mistral, OpenRouter, Azure OpenAI, Ollama (local)
- **Automatic failover** across a configurable provider chain on retryable errors
- **Multi-tenant auth**: tenants, teams, scoped API keys with argon2 hashing
- **Admin CLI** (`pronaos-cli`) for tenant / team / key lifecycle
- **Alembic migrations** working against SQLite (dev) and Postgres (prod) from the same code
- **Request-scoped structured logging** with `request_id`, `tenant_id`, `team_id`, `key_id` bound automatically
- **OpenTelemetry instrumentation** wired across FastAPI, httpx, SQLAlchemy
- **80+ unit tests** + live integration test against real Groq
- **CI pipeline**: ruff strict, mypy strict, pytest with coverage gate, Docker build, Trivy + gitleaks
- **Repository hygiene**: pre-commit hooks, editorconfig, dependabot, conventional commits

On the roadmap:

- **Cost accounting & FinOps dashboards** — Grafana, per-tenant chargeback
- **Audit log** — append-only, hash-chained, tamper-evident
- **Semantic cache** — embedding-based, tenant-isolated
- **Guardrails** — PII redaction, prompt-injection defense, policy engine
- **Evaluation harness** — LLM-as-judge, CI-gated prompt/model regressions
- **Admin UI** — Next.js dashboard for tenants, keys, usage, traces
- **Helm chart + Terraform module** — one-command deploy

---

## License

MIT — see [`LICENSE`](LICENSE).
