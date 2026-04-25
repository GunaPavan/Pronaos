# Pronaos

> Enterprise LLM gateway with first-class observability, cost control, multi-tenancy, and agent-native tracing.

Pronaos sits between your applications and every LLM provider (OpenAI, Anthropic, Bedrock, Gemini, Groq, Mistral, local Ollama, and any OpenAI-compatible endpoint) and gives you a single, governed, observable control plane for all AI traffic.

It is designed to be the **production spine** that enterprise AI teams silently need but rarely build well: unified API, per-tenant cost and quota management, semantic caching, automatic provider failover, PII redaction, prompt-injection defense, SOC2-grade audit logs, OpenTelemetry tracing end-to-end, and an evaluation harness that can gate model or prompt changes in CI.

---

## Why this exists

A 500-person company using LLMs today typically has:

- Multiple teams calling providers directly with ad-hoc API keys
- No unified view of spend, who is spending, or on what
- PII silently leaving the org in prompts
- A single-provider outage taking production down
- No defensible audit trail when legal or compliance ask what the AI said to a customer
- No way to A/B test model or prompt changes with real traffic

Pronaos collapses all of that into one governed hop.

---

## Feature highlights

| Area                    | Capability                                                                     |
| ----------------------- | ------------------------------------------------------------------------------ |
| Universal API           | OpenAI-compatible `/v1/chat/completions`, `/v1/embeddings`, streaming SSE      |
| Provider support        | OpenAI, Anthropic, Bedrock, Gemini, Groq, Mistral, DeepSeek, Ollama, custom    |
| Cost & FinOps           | Per-tenant, per-team, per-key budgets with hard and soft limits, chargeback   |
| Semantic caching        | Embedding-based cache with tenant isolation and configurable TTLs              |
| Automatic failover      | Provider outage? Transparent fallback in < 200 ms                              |
| PII redaction           | Inline strip of SSNs, cards, emails, custom patterns before egress             |
| Prompt-injection defense| Layered classifier + policy engine to detect and block adversarial prompts     |
| Observability           | OpenTelemetry traces, Prometheus metrics, structured logs — end-to-end         |
| Audit log               | Append-only, hash-chained, compliance-ready record of every call               |
| Auth & RBAC             | OIDC/SSO, per-team roles, scoped API keys                                      |
| Multi-tenancy           | Tenant isolation across data, cache, quotas, and observability                 |
| Evaluation harness      | Prompt/model regression tests, LLM-as-judge, CI-gated rollouts                 |
| Agent-native tracing    | Full run trees for agent workflows, not just single-shot completions           |

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

This project is in **week 0 — scaffold only**. See [`ROADMAP.md`](ROADMAP.md) for the 12-week plan.

What works today:

- FastAPI skeleton with `/healthz` and `/readyz`
- Structured logging with `structlog`
- OpenTelemetry instrumentation wired up
- Local development stack via `docker-compose`
- CI pipeline: lint, type-check, tests, container build, security scans
- Repository hygiene: pre-commit, editorconfig, dependabot, conventional commits

What is stubbed but not yet implemented:

- Provider implementations (interface defined, only a mock provider wired up)
- Semantic cache (schema in place, logic pending)
- Guardrails (PII regex scaffold only)
- Audit log writer (interface defined)
- Admin UI

---

## License

MIT — see [`LICENSE`](LICENSE).
