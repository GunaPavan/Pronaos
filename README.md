# Pronaos

> Self-hosted LLM gateway with **two-tier semantic caching**, multi-tenant cost accounting, automatic failover across 12+ providers, and a Grafana-ready observability stack — behind one OpenAI-compatible API.

Pronaos sits between your applications and every supported LLM provider, giving you one governed entry point for every AI call. Today it routes across **12+ providers** — Anthropic (native adapter) plus Groq, OpenAI, DeepSeek, Together, Fireworks, Perplexity, xAI, Cerebras, Mistral, OpenRouter, Azure OpenAI, and Ollama (local) via a single OpenAI-compatible adapter — behind one OpenAI-shape API, with multi-tenant authentication and automatic failover on retryable errors.

**The goal:** become the spine production AI teams need — unified API, per-tenant cost and quota management, semantic caching, PII redaction, prompt-injection defense, hash-chained audit logs, end-to-end OpenTelemetry, and CI-gated evaluation. Some of this ships today; the rest is on the [roadmap](ROADMAP.md).

---

## See it running

`docker compose up -d && uvicorn pronaos.main:app` brings up the whole stack — gateway, Postgres, Redis, Qdrant, Prometheus, Tempo, and two pre-provisioned Grafana dashboards.

### FinOps dashboard — cache effectiveness in real time

![FinOps dashboard showing 65.6% cache hit rate over 60 requests](docs/images/grafana-finops.png)

> Captured live after `python scripts/demo_cache.py --runs 60` against the gateway. The Cache hit rate panel (bottom-left) is the moneyshot: **65.6%** of requests served from cache, no upstream provider call. Hit-rate climbs from 0% on a cold start as the L1 (Redis exact-match) and L2 (Qdrant embedding-similarity at 0.95 cosine threshold) tiers warm up. Spend panels show "No data" because the demo used Groq's free-tier `llama-3.1-8b-instant` ($0/Mtok) — they light up immediately on any paid model.

### Operational dashboard

![Operational dashboard showing request rate, latency p50/p95/p99, and per-provider RPS](docs/images/grafana-overview.png)

> Request rate, error rate, HTTP latency percentiles, and provider-level RPS / p95 latency. The spike at 12:50 is the demo run. Quota denials and error rate panels stay flat — no rate-limit hits, no 5xx — which is the boring-is-good state you want from a gateway in steady state.

### Swagger UI — the public API surface

![Swagger UI listing /v1/healthz, /v1/chat/completions, /v1/admin/usage with full request/response schemas](docs/images/swagger-ui.png)

> Auto-generated from the FastAPI route definitions. Try the chat endpoint interactively at `http://localhost:8080/docs` once the gateway is running.

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
| Cost accounting          | Per-call audit rows, `GET /v1/admin/usage` with filters, `team chargeback` CLI | ✅ shipped    |
| FinOps dashboards        | Grafana panels for spend, burn rate, tokens, per-model breakdown               | ✅ shipped    |
| Prometheus + Grafana     | `/metrics` endpoint, two provisioned dashboards, OTEL collector → Tempo        | ✅ shipped    |
| Semantic caching         | Two-tier (Redis exact-match + Qdrant embedding-based) with per-tenant isolation| ✅ shipped    |
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

Prerequisites: Python 3.12. Docker is optional (only needed for the full
observability stack — Grafana, Tempo, Prometheus).

### On Windows (PowerShell)

```powershell
git clone <your-fork>
cd pronaos
Copy-Item .env.example .env

# One-stop task runner — calls into the venv with UTF-8 set, no `make` needed
.\tasks.cmd install     # creates .venv and installs deps
.\tasks.cmd db-upgrade  # apply Alembic migrations
.\tasks.cmd dev         # start the gateway on :8080

# Other handy tasks:
.\tasks.cmd test        # 230+ unit tests
.\tasks.cmd ci          # lint + typecheck + tests (everything CI runs)
.\tasks.cmd demo        # end-to-end demo against real Groq
.\tasks.cmd             # show all tasks
```

### On macOS / Linux

```bash
git clone <your-fork>
cd pronaos
cp .env.example .env

make install
make db-upgrade   # or: .venv/bin/python -m pronaos.cli db upgrade
make dev
```

Smoke test:

```bash
curl -s http://localhost:8080/v1/healthz
# {"status":"ok","version":"0.1.0"}
```

### Full observability stack

```bash
docker compose up -d     # Postgres, Redis, Qdrant, Prometheus, Grafana, Tempo, OTEL collector
```

Then open **http://localhost:3000** → Dashboards → Pronaos. Two dashboards
ship: `Pronaos — Overview` (request rate, error rate, latency p50/p95/p99,
quota denials by reason) and `Pronaos — FinOps` (USD spend, projected
daily burn, tokens, cache hit rate, spend-by-model). Details in
[`observability/README.md`](observability/README.md).

### Watch the cache work

After issuing an API key (`pronaos-cli key issue --team <id>`), fire
synthetic traffic at the gateway and watch the hit rate climb from 0%:

```bash
python scripts/demo_cache.py --api-key pn_live_... --runs 60
# total:    60
# L1 hits:  29  (exact)
# L2 hits:  2   (semantic paraphrase)
# misses:   29  (forwarded to provider)
# hit rate: 51.7%
```

Open the FinOps dashboard while it runs — the cache panels move live.
See [`scripts/README.md`](scripts/README.md) for details and tuning knobs.

---

## Project status

**In active development** — currently building out provider integrations and admin UI. See [`ROADMAP.md`](ROADMAP.md) for the full plan and [`PLAN.md`](PLAN.md) for the phase-by-phase execution checklist.

What works today:

- **Unified OpenAI-compatible API** (`/v1/chat/completions`, streaming SSE)
- **12+ provider routing** via one generic OpenAI-compat adapter + native Anthropic adapter: Anthropic, OpenAI, Groq, DeepSeek, Together, Fireworks, Perplexity, xAI, Cerebras, Mistral, OpenRouter, Azure OpenAI, Ollama (local)
- **Automatic failover** across a configurable provider chain on retryable errors
- **Multi-tenant auth**: tenants, teams, scoped API keys with argon2 hashing — bidirectional least-privilege scopes (`chat:write`, `admin:usage`)
- **Per-team token budgets + per-key RPS limits** — Redis Lua token-bucket in prod, in-memory in dev
- **Per-call cost accounting**: every successful chat completion writes one `usage_records` row (tenant, team, key, provider, model, tokens, cost, request_id, status)
- **Admin reporting**: `GET /v1/admin/usage` returns paginated rows + aggregate totals over the same filter — tenant-isolated by default
- **Chargeback CLI**: `pronaos-cli team chargeback <team-id>` prints monthly spend broken down by model / provider / status
- **Admin CLI** (`pronaos-cli`) for tenant / team / key / budget / RPS lifecycle
- **Alembic migrations** working against SQLite (dev) and Postgres (prod) from the same code
- **Request-scoped structured logging** with `request_id`, `tenant_id`, `team_id`, `key_id` bound automatically
- **Prometheus `/metrics`** exposing RPS / latency histograms / provider tokens / cost-hcents / quota denials / cache hit rate — see [`observability/README.md`](observability/README.md)
- **Two Grafana dashboards** (Overview + FinOps) auto-provisioned via `docker compose up`
- **OpenTelemetry** instrumentation across FastAPI / httpx / SQLAlchemy plus named spans for `pronaos.quota.check` and `pronaos.provider.call`
- **Two-tier semantic cache** — Redis exact-match (L1) + Qdrant embedding-similarity (L2, sentence-transformers all-MiniLM-L6-v2). Promotion-on-L2-hit, tenant-isolated by construction, fail-open on backend errors
- **230+ unit tests** + live integration test against real Groq
- **CI pipeline**: ruff strict, mypy strict, pytest with coverage gate, Docker build, Trivy + gitleaks
- **Repository hygiene**: pre-commit hooks, editorconfig, dependabot, conventional commits

On the roadmap:

- **Audit log** — append-only, hash-chained, tamper-evident (separate from cost accounting)
- **Guardrails** — PII redaction, prompt-injection defense, policy engine
- **Evaluation harness** — LLM-as-judge, CI-gated prompt/model regressions
- **Admin UI** — Next.js dashboard for tenants, keys, usage, traces
- **Helm chart + Terraform module** — one-command deploy

---

## License

MIT — see [`LICENSE`](LICENSE).
