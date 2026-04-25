# Execution Plan — Pronaos

This is the **operational** plan for building Pronaos. `ROADMAP.md` is the public 12-week narrative; **this file is the internal checklist**. Every phase is atomic, testable, and unlocks the next without rework.

---

## Ground rules (read once, reread often)

1. **Never skip an exit criterion.** If the acceptance test does not pass, the phase is not done — no matter how "close" it feels.
2. **One phase at a time, one sub-task at a time.** No parallel work across phases. Finish, verify, commit, move on.
3. **Every phase ends green.** `make ci` must pass before starting the next phase. Failing tests never carry over.
4. **Scope discipline.** If you notice something out of scope during a phase, write it as a `TODO(phase-N)` comment and move on. Do not expand the current phase.
5. **Demo artefact per phase.** Every phase produces something a human can *see* (a curl output, a Grafana screenshot, a passing test, a video). No "internal refactors" without a visible signal.
6. **Commits are bounded.** One commit per sub-task. Commit message format: `phase-N.M: <imperative summary>`. Never squash away the phase history — it is the portfolio narrative.
7. **Ask before scope-creeping.** If a phase exceeds its day budget by 50%, stop and cut scope rather than pushing through.
8. **No mock-only "done".** Mocks are fine in unit tests, but every phase needs at least one real-dependency integration test (real Postgres, real provider, real Redis) before it's closed.

---

## Phase index (TL;DR)

| #   | Phase                          | Rough budget | Unlocks                                      |
|-----|--------------------------------|--------------|----------------------------------------------|
| 0   | Bootstrap — make the scaffold run | 1 day     | Everything                                   |
| 1   | First provider (Anthropic)     | 3 days       | Real completions                             |
| 2   | OpenAI-compat adapter + catalog + router | 3 days | 12+ providers, multi-provider fallback     |
| 3   | Auth (API keys, tenants)       | 3 days       | Identity & isolation                         |
| 4   | Quotas & rate limits           | 2 days       | FinOps story begins                          |
| 5   | Cost accounting                | 2 days       | Dashboards have numbers                      |
| 6   | Full OTEL pipeline + dashboards| 3 days       | The demo that sells the project              |
| 7   | Audit log with hash chain      | 2 days       | Compliance story                             |
| 8   | Semantic cache                 | 3 days       | Cost-savings story                           |
| 9   | Circuit breaker + failover     | 3 days       | Resilience story (chaos-testable)            |
| 10  | PII redaction                  | 2 days       | Safety story — ingress                       |
| 11  | Prompt-injection defense       | 3 days       | Safety story — adversarial                   |
| 12  | Evaluation harness             | 3 days       | "CI-gated AI" story                          |
| 13  | Admin UI (Next.js)             | 5 days       | Product surface                              |
| 14  | Helm chart + Terraform module  | 3 days       | "One-command deploy"                         |
| 15  | Load + chaos test suite        | 2 days       | Benchmark numbers in README                  |
| 16  | Polish + launch                | 2 days       | Demo video, public link, LinkedIn post       |

Total ≈ 44 working days (≈ 9 weeks at 5 days/week). Slips to 12 weeks with normal friction.

---

## Phase 0 — Bootstrap

**Objective.** The existing scaffold runs end-to-end on your machine. No new features.

### Prerequisites
- Python 3.12 installed (`python --version`)
- Docker Desktop running (`docker ps` works)
- Git configured

### Tasks
1. `cp .env.example .env` — leave defaults.
2. `make install` — venv built, `pip list | grep fastapi` shows 0.115+.
3. `make up` — all 7 containers become healthy. `docker compose ps` shows no `unhealthy`/`restarting`.
4. `make dev` — FastAPI serves on `:8080`.
5. `curl -s http://localhost:8080/v1/healthz | jq` — returns `{"status":"ok","version":"0.1.0"}`.
6. `curl -s http://localhost:8080/v1/readyz | jq` — returns `{"status":"ready"}`.
7. Make one POST to `/v1/chat/completions` with a dummy body — scaffold response comes back.
8. Open Grafana `http://localhost:3000` (admin/admin), confirm Tempo + Prometheus datasources are green.
9. `make ci` — lint, typecheck, tests all pass.

### Exit criteria
All of the above commands produce the expected output. Paste the results of steps 5 and 9 into a scratch note — they're your "before" evidence.

### Explicit non-goals
- No new endpoints, no new code. Just boot.
- No real provider calls yet.

### Risks
- **Port conflicts on 3000/5432/6379/6333/9090/3200/4317.** Kill whatever's already there, don't remap. Mitigation: `docker compose down` first, then `netstat`/`lsof` to find strays.
- **asyncpg wheel fails on Windows.** Mitigation: install Microsoft C++ Build Tools once; it's a known one-time fix.

---

## Phase 1 — First provider (Anthropic)

**Objective.** A real OpenAI SDK pointed at the gateway returns a **real streamed** Claude response.

### Prerequisites
- Phase 0 complete.
- Anthropic API key with at least $5 credit.

### Tasks
1. **1.1** Add `anthropic>=0.40` to `pyproject.toml` dependencies; `make install`.
2. **1.2** Implement `src/pronaos/providers/anthropic.py`:
   - Subclass `Provider`
   - Translate OpenAI-shape request → Anthropic Messages API
   - Implement `chat_completion` for **non-streaming** first
   - Implement `cost_cents` with current Claude pricing (cents-per-token constants at top of file)
3. **1.3** Unit test: `tests/unit/providers/test_anthropic.py` using `respx` to mock the Anthropic HTTP call. Verify request translation, response translation, cost math.
4. **1.4** Replace the scaffold body in `src/pronaos/api/v1/chat.py` with a call to the Anthropic provider (hardcoded for this phase; routing comes in phase 2).
5. **1.5** Add streaming:
   - `stream=True` returns SSE conforming to OpenAI's format
   - Use FastAPI `StreamingResponse`
   - Preserve backpressure (don't buffer the full response)
6. **1.6** Unit test for streaming using `respx` + an iterator of fake SSE events.
7. **1.7** Integration test (marked `@pytest.mark.integration`): hits real Anthropic if `ANTHROPIC_API_KEY` is set in env. Asserts non-empty content, `usage.completion_tokens > 0`.

### Exit criteria
```bash
export ANTHROPIC_API_KEY=sk-ant-...

# 1. Non-streaming
curl -s http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"claude-opus-4-7","messages":[{"role":"user","content":"say hi"}]}' | jq '.choices[0].message.content'
# → non-empty string from real Claude

# 2. Streaming via OpenAI SDK
python - <<'EOF'
from openai import OpenAI
c = OpenAI(api_key="dummy", base_url="http://localhost:8080/v1")
for chunk in c.chat.completions.create(
    model="claude-opus-4-7",
    messages=[{"role":"user","content":"count 1 to 5"}],
    stream=True,
):
    print(chunk.choices[0].delta.content or "", end="", flush=True)
EOF
# → tokens stream to stdout

# 3. CI green
make ci
PRONAOS_ENABLE_INTEGRATION=1 pytest -m integration
```

### Explicit non-goals
- No OpenAI adapter yet.
- No router — `chat.py` can hardcode `AnthropicProvider`.
- No auth, no quotas, no cache.
- Model name translation is a hand-rolled map for now.

### Risks
- **Streaming edge cases** (client disconnect mid-stream → upstream leak). Mitigation: always wrap the provider call in `try/finally` that closes the upstream response.
- **Anthropic rate limit in dev.** Mitigation: cache a handful of respx fixtures; only run the real integration test intentionally.

---

## Phase 2 — OpenAI-compat adapter + provider catalog + router

**Objective.** One generic `OpenAICompatibleProvider` class unlocks 12+ real providers (Groq, DeepSeek, OpenRouter, Together, Fireworks, Perplexity, OpenAI, xAI, Cerebras, Mistral, Azure OpenAI, Ollama, vLLM) with no new code per provider. A router parses `<provider>/<model>` and dispatches. A fallback chain fires on retryable errors.

**Rationale.** The enterprise value prop is "one gateway, many providers." Real products (Portkey, LiteLLM, OpenRouter) exploit the fact that most providers already speak OpenAI-compatible HTTP — they do **not** write a bespoke adapter for each. We match that pattern: native adapters only for providers with non-OpenAI wire formats (Anthropic, Bedrock, Gemini); everyone else is a config block.

### Tasks
1. **2.1** `providers/openai_compat.py` — `OpenAICompatibleProvider`:
   - Config-driven: `base_url`, `api_key`, `default_headers`, `pricing_map`.
   - Non-streaming: POST to `{base_url}/chat/completions`, return chunk.
   - Streaming: **pass SSE through unchanged** — upstream already emits
     `chat.completion.chunk` shape, so translation is a no-op. (This is why
     one adapter covers 12+ providers.)
   - Error classification (401→AuthError, 429→RateLimitError, 5xx→retryable).
2. **2.2** Unit tests (`tests/unit/providers/test_openai_compat.py`):
   - Request shape passthrough
   - Streaming SSE passthrough (bytes level)
   - Error mapping
   - Cost math with configurable pricing map
3. **2.3** `providers/catalog.py` — pre-registered configs for Groq, DeepSeek,
   OpenRouter, Together, Fireworks, Perplexity, xAI, Cerebras, Mistral,
   OpenAI, Azure OpenAI, Ollama. Each entry: name, base URL, key env var,
   pricing, supported models.
4. **2.4** `core/router.py`:
   - Parse `<provider>/<model>` from the request's `model` field.
   - Bare model names route to a configured default (per-tenant later; global for Phase 2).
   - Provider resolution goes through the registry.
5. **2.5** `core/failover.py`:
   - Static fallback chain per route: `[primary, fallback1]`.
   - On retryable error, try next in chain; non-retryable errors (auth, 4xx) do not trigger failover.
   - **Max 1 fallback this phase** — circuit breaker is Phase 9.
6. **2.6** Unit tests for router + failover:
   - Provider resolution for prefixed + bare names
   - Unknown provider → 400
   - Injected retryable error → fallback fires, client sees success
   - Injected auth error → no fallback, client sees 401
7. **2.7** Wire router into `/v1/chat/completions`:
   - Remove the Phase-1 `get_provider_for_model` stub
   - Use `Router` + `FailoverExecutor` on both streaming + non-streaming paths
8. **2.8** Update `providers/registry.py`:
   - Build from catalog: for each catalog entry with a configured API key, eagerly register an instance.
   - Keeps Anthropic native adapter working alongside the generic adapter.
9. **2.9** Live integration test against **Groq** (free tier):
   - `tests/integration/test_groq_live.py`, gated on `GROQ_API_KEY`.
   - Asserts non-streaming + streaming both return real tokens.

### Exit criteria
```bash
# Unit tests: router, failover, openai_compat all green
pytest tests/unit -v

# Live demo — free, via Groq
$env:GROQ_API_KEY = "gsk_..."
$body = '{"model":"groq/llama-3.3-70b-versatile","messages":[{"role":"user","content":"say pong"}]}'
Invoke-RestMethod -Uri http://localhost:8080/v1/chat/completions -Method Post -ContentType "application/json" -Body $body

# Failover proof (unit): mock groq → 503, assert it falls over to a configured secondary
pytest tests/unit/core/test_failover.py -v

# Groq live integration test
pytest -m integration tests/integration/test_groq_live.py -v
```

### Explicit non-goals
- **No native Bedrock or Gemini adapter yet** — Phase 2.5 once AWS/Google
  accounts are available. Architecture is ready for them (same `Provider`
  interface).
- No per-tenant routing (all tenants share config for now).
- No circuit breaker (Phase 9).
- No fallback metrics (Phase 6).

### What the README will be able to claim after Phase 2
| Provider | Status |
|---|---|
| Anthropic | ✅ native |
| OpenAI / Azure OpenAI | ✅ via openai-compat |
| Groq · DeepSeek · OpenRouter · Together · Fireworks · Perplexity · xAI · Cerebras · Mistral | ✅ via openai-compat |
| Ollama / vLLM / any custom endpoint | ✅ via openai-compat |
| AWS Bedrock · Google Gemini | 🔜 Phase 2.5 |

---

## Phase 3 — Auth (API keys, tenants, teams)

**Objective.** Requests without a valid API key are rejected. Valid keys resolve to `(tenant, team, key)` and that context reaches the handler.

### Tasks
1. **3.1** Alembic setup: `alembic.ini`, `migrations/env.py`, first empty migration.
2. **3.2** Models in `db/models.py`: `Tenant`, `Team`, `ApiKey` (hashed, scopes, revoked_at).
3. **3.3** Migration that creates the three tables.
4. **3.4** `scripts/pronaos_cli.py`:
   - `tenant create <name>`
   - `team create --tenant <id> <name>`
   - `key issue --team <id> --scopes chat:write`
   - Keys printed **once** on creation; only the hash stored.
5. **3.5** `auth/api_keys.py`:
   - `verify_key(bearer: str) -> Principal | None`
   - Constant-time comparison; uses `argon2` or `bcrypt`.
6. **3.6** FastAPI dependency `require_principal` used on `/v1/chat/completions`.
7. **3.7** Logging context binds `tenant_id`, `team_id`, `key_id` for the full request.
8. **3.8** Tests:
   - No header → 401
   - Bad key → 401
   - Revoked key → 401
   - Valid key → handler sees correct principal
   - Integration test: spin up Postgres, run migrations, issue key via CLI, call API with it.

### Exit criteria
```bash
# Without key
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8080/v1/chat/completions -d '{...}'  # → 401

# With key
KEY=$(pronaos-cli key issue --team 1 --scopes chat:write | tail -1)
curl ... -H "Authorization: Bearer $KEY" ...  # → 200

# Revoked key
pronaos-cli key revoke <id>
curl ...  # → 401
```

### Explicit non-goals
- No OIDC/SSO yet (phase 13 when the admin UI needs it).
- No scope enforcement beyond "key must exist" (phase 4 will use scopes for quotas).

---

## Phase 4 — Quotas & rate limits

**Objective.** Bursts and monthly budgets are enforced per key/team/tenant.

### Tasks
1. **4.1** Redis connection + health check on startup.
2. **4.2** Token-bucket limiter `billing/ratelimit.py`: `check_and_consume(scope_key, cost) -> Allowed | Denied(retry_after)`.
3. **4.3** Per-key RPS limit (default 10 r/s, configurable per key in DB).
4. **4.4** Per-team monthly **token** budget (counter in Redis with monthly rollover, authoritative ledger in Postgres).
5. **4.5** Middleware order: auth → quota → handler.
6. **4.6** On 429, return `Retry-After` header + structured error body.
7. **4.7** Tests:
   - Burst test: 20 req in 1s on a key limited to 10 r/s → exactly 10 succeed.
   - Monthly budget exhaustion → 429 with `reason: "monthly_token_budget"`.
   - Key with unlimited scope → bypasses rate limit.

### Exit criteria
```bash
# Burst test
seq 20 | xargs -P 20 -I {} curl -s -o /dev/null -w "%{http_code}\n" \
  -H "Authorization: Bearer $KEY" http://localhost:8080/v1/chat/completions \
  -d '{"model":"anthropic/claude-opus-4-7","messages":[...]}' | sort | uniq -c
# → 10 "200", 10 "429" (approximate)
```

### Explicit non-goals
- No cost-based budgets yet — token-based only. (Cost comes in phase 5.)
- No cache bypass paths yet.

---

## Phase 5 — Cost accounting

**Objective.** Every call records a precise cost; an admin endpoint reports spend by tenant/team/model.

### Tasks
1. **5.1** Pricing table in `billing/pricing.py` — dict of `(provider, model) -> (prompt_cents_per_mtok, completion_cents_per_mtok)`. Keep it data; don't bury in code paths.
2. **5.2** `usage_records` table: `id, ts, tenant_id, team_id, key_id, provider, model, prompt_tokens, completion_tokens, cost_cents, request_id`.
3. **5.3** On every successful completion, write a usage record (async; don't block response).
4. **5.4** Extend quota: per-team **monthly cost budget** (cents) alongside token budget.
5. **5.5** `GET /v1/admin/usage?tenant=...&from=...&to=...` returns JSON summary.
6. **5.6** Tests:
   - Call Anthropic with mock returning 100/200 tokens → usage row has expected cents.
   - Aggregation returns correct totals across multiple records.
   - Cost budget triggers 429 when exceeded.

### Exit criteria
```bash
# After 10 test calls
curl -H "Authorization: Bearer $ADMIN_KEY" \
  "http://localhost:8080/v1/admin/usage?tenant=1" | jq
# → JSON showing total_cost_cents, breakdown by model
# Numbers match expected pricing × token count to the cent.
```

### Explicit non-goals
- No UI — JSON only (UI comes in phase 13).
- No chargeback / invoicing (out of scope).

---

## Phase 6 — Full OTEL pipeline + dashboards

**Objective.** Every request produces a full trace tree visible in Tempo. A Grafana dashboard shows RPS, latency, error rate, cost/sec, cache hit, all live.

### Tasks
1. **6.1** Spans: `pronaos.request`, `pronaos.auth`, `pronaos.quota`, `pronaos.cache.lookup`, `pronaos.router`, `pronaos.provider.call`, `pronaos.audit.write`. Nested under a single root per request.
2. **6.2** Span attributes: `tenant_id`, `team_id`, `model`, `provider`, `prompt_tokens`, `completion_tokens`, `cost_cents`, `cache.hit`, `fallback.used`.
3. **6.3** Prometheus metrics:
   - `pronaos_requests_total{tenant,model,provider,status}` (counter)
   - `pronaos_request_duration_seconds` (histogram, labels as above)
   - `pronaos_cost_cents_total{tenant,model,provider}` (counter)
   - `pronaos_provider_error_rate` (gauge, computed in recording rule)
4. **6.4** `X-Pronaos-Trace-Id` response header with W3C trace id.
5. **6.5** Grafana dashboard JSON committed under `observability/grafana/dashboards/pronaos-overview.json`. Provisioned automatically.
6. **6.6** One dashboard row per story: RPS, latency, errors, providers, cost, caches, quotas.

### Exit criteria
```bash
# Pick a recent request's trace id from response header, open in Tempo.
# Span tree: pronaos.request → auth → quota → router → provider.call (all nested, all attributes populated).

# Grafana: open "Pronaos / Overview" dashboard — every panel has live data after running some load.
```
Capture a screenshot of the Grafana dashboard and a screenshot of the trace — they go in the README.

### Explicit non-goals
- No per-tenant dashboards (phase 13 UI can do that).
- No alerting yet (document in runbooks, implement optionally in phase 15).

---

## Phase 7 — Audit log with hash chain

**Objective.** An append-only, tamper-evident record of every call, verifiable by a standalone script.

### Tasks
1. **7.1** `audit_log` table: `id, ts, tenant_id, principal, request_hash, response_hash, prev_hash, this_hash, cost_cents, trace_id`.
2. **7.2** Hash definition: `this_hash = sha256(prev_hash || canonical_json(row_without_this_hash))`.
3. **7.3** Async writer: enqueued on request completion, persisted on a background task. If DB down, buffer in memory up to N entries, then refuse requests (fail-closed).
4. **7.4** `GET /v1/admin/audit` with tenant scoping + pagination.
5. **7.5** `scripts/audit_verify.py`: walks the chain for a tenant, recomputes each hash, fails on mismatch.
6. **7.6** Tests:
   - Normal path: N requests → N rows, chain verifies.
   - Tamper: manually UPDATE a row → verify script exits non-zero with the offending row id.
   - Missing row: DELETE a row → verify script flags the broken link.

### Exit criteria
```bash
# Happy path
for i in $(seq 1 50); do curl ... ; done
python scripts/audit_verify.py --tenant 1
# → "OK: 50 entries verified"

# Tamper path
psql "$PRONAOS_DATABASE_URL" -c "UPDATE audit_log SET cost_cents = 0 WHERE id = 25;"
python scripts/audit_verify.py --tenant 1
# → exits 1, prints "BROKEN at id=25"
```

### Explicit non-goals
- No S3 export (document as future work).
- No per-row redaction of PII in audit log (policy decision deferred).

---

## Phase 8 — Semantic cache

**Objective.** Requests whose prompt is semantically close to a prior one return from cache, with strict tenant isolation.

### Tasks
1. **8.1** Qdrant client wrapper; one collection per tenant (`pronaos_cache_<tenant_id>`), lazy created.
2. **8.2** Cheap embedding model (`text-embedding-3-small` or BGE; pluggable).
3. **8.3** On ingress: embed last user message → search Qdrant → if similarity ≥ threshold (default 0.92, per-tenant), return cached response.
4. **8.4** On egress: store `(embedding, request_hash, response, ttl)` unless the prompt contained PII (from phase 10 — skip write if `pii.detected = true`).
5. **8.5** Cache TTL per-tenant configurable.
6. **8.6** Tenant isolation test: seed tenant A's cache, call from tenant B with identical prompt → miss.
7. **8.7** Metrics: `pronaos_cache_hit_total`, `pronaos_cache_similarity_histogram`.

### Exit criteria
```bash
# Measure effect
make benchmark-cache
# Script runs 100 requests, 70% of which are paraphrases.
# Report: hits=X%, savings=$Y, p50_hit_latency=Z_ms (should be < 20ms).

# Isolation
pytest tests/integration/test_cache_isolation.py
# → passes
```

### Explicit non-goals
- No cache invalidation by content change (TTL only).
- No cross-model cache (key includes provider/model).

---

## Phase 9 — Circuit breaker + failover

**Objective.** Real provider outage is survived automatically; client never sees 5xx unless *all* fallbacks are exhausted.

### Tasks
1. **9.1** `core/circuit_breaker.py`: states `closed | open | half_open`, configurable thresholds (error rate over sliding window), cooldown.
2. **9.2** One breaker per `(provider, model)`.
3. **9.3** Router: check breaker state before dispatch; skip open providers.
4. **9.4** Half-open probe: single request allowed every cooldown interval to test recovery.
5. **9.5** Chaos test harness: `tests/chaos/test_provider_outage.py`:
   - Start gateway with fake provider A (primary) that fails.
   - Fake provider B (fallback) succeeds.
   - Send 100 requests. Assert: 0 failures, fallback used on first miss, breaker opens within N failures, recovers after cooldown.
6. **9.6** Metric: `pronaos_circuit_state{provider,model}` + `pronaos_fallback_total{from,to}`.

### Exit criteria
```bash
pytest tests/chaos/ -v
# All pass. Capture the test's log output — it's your "chaos" demo reel.
```
Record a short Grafana video: toggle the fake provider from healthy to failing → watch breaker open → fallback takes over → breaker closes on recovery.

### Explicit non-goals
- No circuit breaker on Redis/Postgres (phase 15 covers these).
- No active health probing independent of traffic.

---

## Phase 10 — PII redaction

**Objective.** PII (SSN, credit cards, emails, phone numbers) never reaches the provider.

### Tasks
1. **10.1** `guardrails/pii.py`: regex rules for US SSN, E.164 phone, email, Luhn-valid credit cards. Each rule returns spans `[(start, end, label)]`.
2. **10.2** Optional Presidio integration behind `PRONAOS_PRESIDIO_ENABLED=true`. Adds NER-based detection.
3. **10.3** Redaction function: replace spans with `[REDACTED:<label>]`. Also emit event.
4. **10.4** Ingress middleware: redact message content before router dispatch; attach metadata `pii.detected=true` on the request context (used by cache writer in phase 8).
5. **10.5** Audit log records redaction event (type + count, **never content**).
6. **10.6** Test suite: 50 prompts with embedded PII; assert none leaves the process (verified with httpx request interceptor).

### Exit criteria
```bash
pytest tests/unit/guardrails/test_pii.py -v   # 50/50 pass
# Manual: curl with "my SSN is 123-45-6789" and trace the span — upstream body shows [REDACTED:SSN].
```

### Explicit non-goals
- Only English patterns; no internationalisation yet.
- No free-form PII extraction from URLs or images.

---

## Phase 11 — Prompt-injection defense

**Objective.** Detect and block common injection / jailbreak attempts; tenant policy decides block vs flag.

### Tasks
1. **11.1** Rule detector: keyword patterns (e.g. "ignore previous", "system prompt", indirect tool invocation attempts), each with weight.
2. **11.2** Optional LLM-as-judge fallback (`claude-haiku-4-5` or Groq) triggered when rules return ambiguous score.
3. **11.3** Policy engine per tenant: `block | flag | allow`; default `flag`.
4. **11.4** Blocked requests return 400 with structured reason; flagged requests pass through but are tagged in audit log.
5. **11.5** Red-team corpus in `tests/redteam/suite.jsonl` — 100 labelled attempts, 50 benign. CI job reports detection rate.
6. **11.6** Metric: `pronaos_injection_total{action}`.

### Exit criteria
```bash
pytest tests/redteam/ -v
# Detection ≥ 90%, false positive on benigns ≤ 5%.
```

### Explicit non-goals
- No adversarial fine-tuning of the detector.
- No image / multimodal injection (text only).

---

## Phase 12 — Evaluation harness

**Objective.** A promoted prompt or model must pass an offline evaluation before merge.

### Tasks
1. **12.1** Suite format: JSONL with `{id, input, reference?, rubric}`.
2. **12.2** `eval/runner.py`: run suite against a named route; record per-example score + overall.
3. **12.3** LLM-as-judge: prompt in `eval/judges/`, model configurable.
4. **12.4** Baseline snapshot: `eval/baselines/<suite>.json`. `eval run --compare` diff against baseline.
5. **12.5** CI job: on PR, run `qa-smoke` suite (20 examples); fail if score drops > 3%.
6. **12.6** `pronaos-cli eval list`, `eval run <suite>`, `eval diff <run-a> <run-b>`.

### Exit criteria
```bash
pronaos-cli eval run suites/qa-smoke.yaml --route default
# Exits 0 with a score. Second run with degraded model shows regression in diff.
```

### Explicit non-goals
- No human-in-the-loop labelling UI.
- No A/B live traffic split (would belong to a router feature, phase 2+).

---

## Phase 13 — Admin UI (Next.js)

**Objective.** Non-engineers can manage tenants, keys, quotas, and see usage/traces.

### Tasks
1. **13.1** `web/` app: Next.js 15, TanStack Query, shadcn/ui.
2. **13.2** Auth: NextAuth with OIDC (Auth0 or Keycloak in dev).
3. **13.3** Pages: Tenants, Teams, Keys, Usage, Traces (link to Tempo), Audit.
4. **13.4** Server actions → gateway admin API (phase 3/5/7 endpoints).
5. **13.5** Role-based UI: admin vs viewer.
6. **13.6** One Playwright smoke test: login → create tenant → issue key → see usage after a test call.

### Exit criteria
```bash
cd web && pnpm test:e2e
# Playwright smoke test passes.
# Manual walkthrough: the flow above works against a live local gateway.
```

### Explicit non-goals
- No per-user preferences.
- No editing prompts/policies in the UI (admin API only for now).

---

## Phase 14 — Helm chart + Terraform module

**Objective.** One command deploys the whole stack; infra can be stood up from zero.

### Tasks
1. **14.1** Helm chart under `deploy/helm/pronaos/`: deployment, service, hpa, ingress, configmap, secret, serviceMonitor.
2. **14.2** `values.yaml` with sane defaults; `values-staging.yaml`, `values-prod.yaml`.
3. **14.3** Terraform module `deploy/terraform/aws/`: EKS (or cheaper ECS Fargate for demo), RDS Aurora Postgres, ElastiCache Redis, S3 bucket for audit export. Variables for scale.
4. **14.4** Smoke deploy on **Kind** locally (cheap, reproducible):
   - `kind create cluster`
   - `helm install pronaos deploy/helm/pronaos`
   - Port-forward, hit `/v1/healthz` → 200.

### Exit criteria
```bash
kind create cluster && helm install pronaos deploy/helm/pronaos -f deploy/helm/pronaos/values-local.yaml
kubectl port-forward svc/pronaos 8080:8080 &
curl http://localhost:8080/v1/healthz  # → {"status":"ok",...}
```
Record the command sequence → a README "deploy in 60 seconds" block.

### Explicit non-goals
- No actual AWS deploy required (cost). Terraform only needs `terraform plan` to pass.
- No multi-region.

---

## Phase 15 — Load + chaos test suite

**Objective.** Public benchmark numbers in the README, produced by reproducible scripts.

### Tasks
1. **15.1** `tests/load/k6-chat.js`: stages ramping 0 → 1000 RPS for 5 minutes against a mock provider.
2. **15.2** Results report auto-generated in `docs/benchmarks/` (p50/p95/p99, error rate, cost/min).
3. **15.3** Chaos matrix in `tests/chaos/`:
   - kill Redis → cache degrades, quotas fail-closed, still returns
   - kill Qdrant → cache disabled, still returns
   - kill Postgres → audit buffer, then graceful 503 after threshold
   - kill primary provider → fallback (phase 9 test expanded)
4. **15.4** CI lightweight: a scaled-down 100 RPS × 60s smoke on PRs.

### Exit criteria
```bash
make loadtest
# Report dropped in docs/benchmarks/YYYY-MM-DD.md:
# - 1000 RPS sustained
# - p50 overhead < 10ms
# - p99 < 150ms
# - 0 errors at steady state
pytest tests/chaos/ -v   # all pass
```

---

## Phase 16 — Polish + launch

**Objective.** The repo is a shareable artefact. Recruiters can grok it in 90 seconds.

### Tasks
1. **16.1** Architecture diagram (mermaid or excalidraw) embedded in README.
2. **16.2** 2-minute screen-recorded walkthrough. Script:
   - 10s pitch
   - 20s "one command to run it"
   - 30s request flow trace in Tempo
   - 30s chaos demo (kill provider → fallback)
   - 20s cost dashboard
   - 10s "here's the repo"
3. **16.3** Public demo link (deploy to Fly.io or Railway; use fake providers to avoid API cost).
4. **16.4** README badges: CI status, coverage, licence, docker pulls.
5. **16.5** Polished LinkedIn/X post + pinned to profile.
6. **16.6** Update portfolio site + CV bullet.

### Exit criteria
Link the video from the README, link the demo URL from the README, post the LinkedIn update. Mark project as v1.0 with a release tag.

---

## When to deviate

The only legitimate reasons to deviate from this plan:

1. **A phase becomes unnecessary.** (Example: you discover a Redis alternative ships quotas for free — skip phase 4's limiter implementation, keep its tests.)
2. **A phase splits cleanly.** (Example: phase 13 UI turns into "auth pages" + "data pages" — fine to split and ship half.)
3. **A phase is blocked by a hard external dep.** (Example: no Anthropic credit → use OpenAI in phase 1, swap later.)

Not legitimate reasons: "bored," "this seems more fun," "let me refactor." Finish the phase. The discipline is the portfolio signal.
