# Pronaos

> Self-hosted LLM gateway with **five empirical claims about its own behavior**, every one verified by a reproducible script.

Pronaos sits between your applications and 12+ LLM providers (Anthropic, OpenAI, Groq, DeepSeek, Together, Fireworks, Perplexity, xAI, Cerebras, Mistral, OpenRouter, Azure OpenAI, Ollama) behind one OpenAI-compatible API — with multi-tenant auth, cost accounting, semantic caching, PII redaction, and a hash-chained audit log. **The unusual part:** it ships with experiments that *measure* each of those features against a real model and a real judge, and prints the numbers.

---

## What this gateway can prove about itself

Five empirical claims, each backed by a script you can run against the live gateway:

| # | Claim | Headline | Reproduce |
| --- | --- | ---: | --- |
| 1 | **L1 cache faithfulness** | Δ = **0.0000** across 8 cases; 46% faster wall-clock | `python scripts/eval_cache_quality.py` |
| 2 | **Semantic cache trades nothing for paraphrase hits** | 12.5% → **87.5%** L2 hit rate as threshold drops; Δ = 0 either way | `python scripts/eval_paraphrase_cache_quality.py` |
| 3 | **Redaction breaks the model when PII is topically relevant** — and per-tenant policy fixes it | tcp_vs_udp: 1.00 → **0.00** under redaction; → **1.00** after `--disable pii.ipv4` | `python scripts/eval_guardrail_quality.py` |
| 4 | **9.3× cost premium bought zero quality gain** on this workload | 8B vs Llama-4 Scout: identical 8/8 pass-rate, $0.000050 vs $0.000463 per call | `python scripts/eval_cost_quality.py` |
| 5 | **Tamper detection works on the live audit log** | `audit verify` exits 0 on intact chain, exits 1 with exact byte diff on tamper | `pronaos-cli audit verify --tenant <id>` |

The full write-ups with terminal output, screenshots, and methodology live in [**See it running**](#see-it-running) below. Most "I built an LLM gateway" portfolios stop at *"the cache exists."* This one closes the loop: built it → measured it → found a real failure (claim #3) → shipped per-team mitigation → re-verified the regression is gone. That's the **engineering arc** the rest of the README documents.

---

## See it running

`docker compose up -d && uvicorn pronaos.main:app` brings the whole stack online — gateway, Postgres, Redis, Qdrant, Prometheus, Grafana, Tempo, OTEL collector — plus two pre-provisioned Grafana dashboards.

### Visual: the FinOps dashboard during a cache demo

![FinOps dashboard showing 65.6% cache hit rate over 60 requests](docs/images/grafana-finops.png)

Captured live during `python scripts/demo_cache.py --runs 60`. The Cache hit rate panel (bottom-left) reads **65.6%** — that fraction of requests was served without ever touching a provider. Spend panels read "No data" because the demo used Groq's free-tier 8B at $0/Mtok; they populate immediately on any paid model.

### Empirical claim #1 — L1 cache faithfulness

[`scripts/eval_cache_quality.py`](scripts/eval_cache_quality.py) clears Redis + Qdrant, runs the eval suite, then re-runs the same suite against the now-warmed cache. If the cache is correct, the second-run scores must equal the first-run scores byte-for-byte.

```text
[2/3] fresh run (8 cases, every request → provider)...
      mean: 1.000  scored: 8/8  wall: 21.8s
[3/3] cached run (8 cases, expecting L1 hits)...
      mean: 1.000  scored: 8/8  wall: 11.8s

max |Δ|: 0.0000      cases over ε: 0 / 8
✅ CLAIM HOLDS: cache preserves quality.
```

The 21.8s → 11.8s wall-clock drop (~46% faster) is the cache short-circuiting every provider call. Quality preserved exactly; latency drops measurably.

### Empirical claim #2 — semantic cache trades nothing for paraphrase hits

[`scripts/eval_paraphrase_cache_quality.py`](scripts/eval_paraphrase_cache_quality.py) asks the harder question: when the user re-asks the same intent in *different words*, does the L2 cache serve a cached response, and does that response still score against the rubric?

Same eval suite, varying only `PRONAOS_SEMANTIC_CACHE_THRESHOLD`:

| Threshold | L2 hit rate | Max per-case Δ | Verdict |
| --- | --- | --- | --- |
| **0.95 (default)** | 12.5% (1/8) | **0.0000** | Conservative: only near-identical paraphrases hit |
| **0.85** | **87.5%** (7/8) | **0.0000** | Permissive: most paraphrases hit. Quality still preserved. |

At threshold 0.85, the gateway returns a single stored response for seven different phrasings of the same intent (e.g. *"What's the average-case time complexity of quicksort?"* and *"What's quicksort's average runtime complexity?"*) and the judge scores all 7 responses identically against the rubric. Both modes preserved quality, so the threshold is a pure hit-rate-vs-false-positive-tolerance dial — **not a hit-rate-vs-quality dial.** That's the headline FinOps result.

### Empirical claim #3 — redaction breaks the model on topically-relevant PII

[`scripts/eval_guardrail_quality.py`](scripts/eval_guardrail_quality.py) injects incidental PII (email, phone, SSN, credit card, IP) into each rubric prompt and runs the eval twice — once clean, once with PII (redacted before reaching the provider). Same rubric grades both.

```text
case                   clean redact      Δ  redacted rules
capital_france          1.00   1.00  +0.00  pii.email
quicksort_avg           1.00   1.00  +0.00  pii.email
refuse_benign           1.00   1.00  +0.00  pii.credit_card
refuse_harmful          1.00   1.00  +0.00  pii.email
simple_arithmetic       1.00   1.00  +0.00  pii.email
speed_of_light          1.00   1.00  +0.00  pii.phone
tcp_vs_udp              1.00   0.00  -1.00  pii.ipv4   ⚠
transformer_summary     1.00   1.00  +0.00  pii.ssn
```

**Seven of eight cases unaffected.** Incidental PII redacts cleanly. **One case catastrophically fails:** the TCP/UDP question included office IPs as setup context. The model receives:

> Our office IPs are `[REDACTED-IP]` and `[REDACTED-IP]`. Give a one-sentence summary of the main difference between TCP and UDP.

…and replies *"I can't provide information that could be used to identify your office's IP addresses."* It refuses the networking question entirely. **Redaction turned a benign prompt into one the model treats as suspicious.**

#### Mitigation: per-team guardrail policy

`Team.guardrail_policy` is a JSON column resolved at request time. CLI to manage it:

```bash
pronaos-cli team set-guardrail-policy <team-id> --disable pii.ipv4
```

After applying that policy and re-running the same experiment:

| | Before policy | After `--disable pii.ipv4` |
| --- | --- | --- |
| tcp_vs_udp score | 1.00 → **0.00** ⚠ | **1.00** ✅ |
| Redacted mean | 0.875 | **1.000** |
| Max \|Δ\| | 1.0000 | **0.0000** |

**The engineering arc:** built the guardrail → measured it → identified a real failure mode → shipped per-team mitigation → re-verified the regression is gone. Most "we shipped safety" claims stop at step 1.

### Empirical claim #4 — 9.3× cost premium bought zero quality gain

[`scripts/eval_cost_quality.py`](scripts/eval_cost_quality.py) sweeps the same eval suite across multiple candidate models, holding the judge constant (Groq's 70B-versatile, kept out of the candidate list to avoid self-grading). For each model it reads the gateway's authoritative per-call cost and computes **dollars per correct answer**.

| Model | Mean score | Pass rate | $ / call | $ / correct |
| --- | ---: | ---: | ---: | ---: |
| `groq/llama-3.1-8b-instant` | 1.000 | 8/8 | $0.000050 | $0.000050 |
| `groq/meta-llama/llama-4-scout-17b-16e-instruct` | 1.000 | 8/8 | $0.000463 | $0.000463 |

Llama 4 Scout costs **9.3× more per call** than the 8B and delivers **identical quality** on this workload. Defaulting to the "better" model wastes ~89% of the spend with no quality gain. On a workload of one million calls, that's **$413 in pure overpayment.**

Important caveats: 8-case golden set; harder workloads would likely differentiate. The point isn't *"always pick 8B"* — it's *"measure before you default."*

### Empirical claim #5 — hash-chained audit + tamper detection

Every successful chat call writes an `AuditRecord` whose `this_hash` is `sha256(prev_hash | request_id | tenant_id | team_id | key_id | provider | model | ts | request_hash | response_hash)`. Chain is per-tenant; bodies are **not** stored (only their hashes), so the audit log doesn't re-create the PII problem guardrails exist to solve.

```text
$ pronaos-cli audit verify --tenant 1743243380104bf4839758939077621e
total records:    4   verified: 4   breaks: 0
chain intact (4 records verified)
$ echo $?
0
```

Mutate one row's `response_hash` directly in the DB and re-verify:

```text
total records:    4   verified: 3   breaks: 1

CHAIN BROKEN — first 5 breaks:
  - record d273f7dd... @ 2026-05-17T12:35:30: hash_mismatch
      expected: 95cce3a70c1e003f...
      actual:   be1539ef1509f7d8...
$ echo $?
1
```

The verifier finds the exact row, returns exit code 1, ready to wire into a nightly CI check.

**Plus a real bug the tests caught.** While building this I hit the SQLite tz-drop trap: writer hashed a tz-aware `ts.isoformat()` (`"...+00:00"`), verifier read the value back from SQLite as naive (no suffix), produced a different `.isoformat()`, **100% chain breakage on every record**. Unit tests caught it before any record ever shipped. Fix: `canonical_ts()` normalises to naive UTC on both sides. That round-trip test is in the audit suite.

This is what auditability looks like when it's real: tamper-evident record, exit-code-aware verifier, AND a test suite that catches the round-trip bugs that would silently invalidate every audit claim.

### Operational view + Swagger

![Pronaos Overview Grafana dashboard](docs/images/grafana-overview.png)

Request rate, error rate, HTTP latency p50/p95/p99, per-provider RPS + p95 latency, quota denials by reason, **guardrail hits per minute by rule** (bottom panel). The spike at ~12:50 is the cache demo run.

![Swagger UI listing /v1/healthz, /v1/chat/completions, /v1/admin/usage](docs/images/swagger-ui.png)

Auto-generated from FastAPI route definitions — try the chat endpoint at `http://localhost:8080/docs`.

---

## Feature highlights

| Area | Capability | Status |
| --- | --- | --- |
| Universal API | OpenAI-compatible `/v1/chat/completions`, streaming SSE | ✅ shipped |
| Provider support | 12+ providers via native Anthropic + generic OpenAI-compat adapter | ✅ shipped |
| Routing & failover | Prefix-based selection; automatic retry across configured chain | ✅ shipped |
| Multi-tenancy | Tenants, teams, scoped API keys (argon2 hashing); bidirectional least-privilege scopes | ✅ shipped |
| Rate limits | Per-key RPS token bucket — in-memory (dev) / Redis Lua (prod) | ✅ shipped |
| Token + cost budgets | Per-team monthly limits with calendar-month rollover, atomic SQL writes | ✅ shipped |
| Cost accounting | Per-call audit rows, `GET /v1/admin/usage` with filters, `team chargeback` CLI | ✅ shipped |
| Prometheus + Grafana | `/metrics` endpoint, two provisioned dashboards, OTEL → Tempo | ✅ shipped |
| OpenTelemetry | FastAPI / httpx / SQLAlchemy + named spans for `pronaos.quota.check` and `pronaos.provider.call` | ✅ shipped |
| Semantic caching | Two-tier (Redis exact-match + Qdrant embedding-based), tenant-isolated by construction | ✅ shipped |
| Guardrails | PII redaction (5 rules + Luhn) + prompt-injection detection, ingress + egress + streaming-aware | ✅ shipped |
| Per-team policy | `Team.guardrail_policy` lets ops disable specific rules per tenant | ✅ shipped |
| Eval harness | LLM-as-judge scorer, YAML golden sets, CLI runner, four bundled experiments | ✅ shipped |
| Audit log | Per-tenant hash-chained record; `pronaos-cli audit verify` walks the chain | ✅ shipped |
| Admin CLI | `pronaos-cli` for tenant / team / key / budget / policy / audit lifecycle | ✅ shipped |
| Admin UI | Next.js dashboard for tenants, keys, usage, traces | 🔜 roadmap |
| OIDC / SSO | Keycloak / Auth0 / Azure AD for human + admin access | 🔜 roadmap |
| Deploy | Helm chart + Terraform module for one-command production install | 🔜 roadmap |

---

## Architecture

```
client app ─► Pronaos ─► [ auth ─► quotas ─► guardrails ─► cache ─► router ─► provider ]
                │                                                              │
                ├─► OTEL collector ─► Tempo / Prometheus / Grafana             ├─► Anthropic
                ├─► Postgres (tenants, keys, quotas, usage, audit)             ├─► Groq / OpenAI
                └─► Redis + Qdrant (L1 cache + L2 semantic cache, rate limits) └─► Bedrock / Gemini / …
```

Every request: auth → quota check → ingress guardrails → cache lookup → routing → provider call → egress guardrails → cache write → audit append → observability export. Streaming responses get the same coverage (egress + audit run at stream close).

Deeper deep-dive in [`ARCHITECTURE.md`](ARCHITECTURE.md).

---

## Quickstart

Prerequisites: Python 3.12. Docker is optional (only for the full observability stack).

```bash
git clone https://github.com/GunaPavan/Pronaos.git
cd Pronaos
cp .env.example .env       # or `Copy-Item .env.example .env` on Windows

# Cross-platform task runner (Windows: tasks.cmd / macOS+Linux: make):
./tasks.cmd install        # creates .venv and installs deps   (or: make install)
./tasks.cmd db-upgrade     # apply Alembic migrations          (or: make db-upgrade)
./tasks.cmd dev            # start the gateway on :8080        (or: make dev)
./tasks.cmd test           # 310+ unit tests
```

Smoke test: `curl -s http://localhost:8080/v1/healthz` → `{"status":"ok","version":"0.1.0"}`.

### Full observability stack

```bash
docker compose up -d   # Postgres, Redis, Qdrant, Prometheus, Grafana, Tempo, OTEL collector
```

Open **http://localhost:3000** → Dashboards → **Pronaos**. Two dashboards ship: `Overview` (RPS, error rate, p50/p95/p99 latency, quota denials, guardrail hits) and `FinOps` (USD spend, projected daily burn, tokens, cache hit rate). Details in [`observability/README.md`](observability/README.md).

### Run the demos

Mint an API key once (`pronaos-cli key issue --team <team-id>`), then:

```bash
python scripts/demo_cache.py --api-key pn_live_... --runs 60          # cache effectiveness
python scripts/demo_guardrails.py --api-key pn_live_...               # PII redaction + injection
pronaos-cli eval run -g tests/eval/data/basic.yaml \
    -c groq/llama-3.1-8b-instant -j groq/llama-3.3-70b-versatile \
    -k pn_live_...                                                     # eval harness
```

Then the four experiments backing claims #1–#4:

```bash
python scripts/eval_cache_quality.py          --api-key pn_live_...   # claim #1
python scripts/eval_paraphrase_cache_quality.py --api-key pn_live_...   # claim #2
python scripts/eval_guardrail_quality.py      --api-key pn_live_...   # claim #3
python scripts/eval_cost_quality.py           --api-key pn_live_...   # claim #4
```

Full details in [`scripts/README.md`](scripts/README.md).

---

## What's left

Active roadmap items (everything else in the table above is shipped):

- **Admin UI** — Next.js dashboard for tenants, keys, usage, traces
- **Tool / function calling** — uniform tool-use shape across providers
- **Multi-judge eval** — Anthropic + Groq concurrent grading for inter-judge agreement
- **Cost-aware routing** — close the loop on claim #4: route by `$/correct` policy
- **Helm + Terraform** — one-command production deploy
- **OIDC / SSO** — Keycloak / Auth0 / Azure AD for human access

See [`ROADMAP.md`](ROADMAP.md) and [`PLAN.md`](PLAN.md).

---

## License

MIT — see [`LICENSE`](LICENSE).
