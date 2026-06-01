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
| `pronaos_routing_decisions_total`            | counter   | strategy, selected_model     |
| `pronaos_hedge_triggered_total`              | counter   | primary, hedge               |
| `pronaos_hedge_wins_total`                   | counter   | winner_provider, role        |
| `pronaos_hedge_cancelled_total`              | counter   | cancelled_provider           |
| `pronaos_cache_stream_replays_total`         | counter   | tier                         |
| `pronaos_ab_decisions_total`                 | counter   | test_id, arm                 |
| `pronaos_agent_turn_denials_total`           | counter   | reason                       |
| `pronaos_embedding_requests_total`           | counter   | provider, model, status      |
| `pronaos_embedding_request_duration_seconds` | histogram | provider, model              |
| `pronaos_embedding_tokens_total`             | counter   | provider, model              |
| `pronaos_embedding_cache_hits_total`         | counter   | model                        |
| `pronaos_rerank_requests_total`              | counter   | provider, model, status      |
| `pronaos_rerank_request_duration_seconds`    | histogram | provider, model              |
| `pronaos_rerank_cache_hits_total`            | counter   | model                        |
| `pronaos_singleflight_followers_total`       | counter   | endpoint                     |
| `pronaos_prompt_cache_tokens_total`          | counter   | provider, model, type        |

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

**Bypassed automatically when:** `temperature > 0` (caller asked for
variety) or the request includes a tool-result message (agent-loop
turn). Phase 28 made the cache **streaming-aware**: `stream=true` is
no longer a bypass condition — streaming calls capture chunk timing
on miss and replay from cache on hit. Bypass paths still increment
`pronaos_cache_lookups_total{result="skip"}` so the dashboard hit-rate
math (`hits / (hits + miss)`) stays honest.

**Streaming cache replay (Phase 28):** the SSE generator captures
`(text, inter_chunk_delay_ms)` pairs and writes them as
`pronaos.stream_chunks` into the cached entry. On a streaming cache
hit, the gateway returns a `StreamingResponse` whose generator walks
the stored chunks, sleeps the original inter-chunk delays, and emits
SSE events. The first chunk's stored delay is deliberately skipped —
the cache exists to eliminate the upstream's time-to-first-token, not
reproduce it. Streaming replays surface as `X-Pronaos-Cache:
hit:replay` on the response and increment
`pronaos_cache_stream_replays_total{tier}` (separate counter from the
generic hit counter so dashboards can split UX-relevant streaming
wins from non-streaming hits).

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
| `presidio` | ML-based PII (opt-in; emits per-entity hits like `presidio.PERSON`) | REDACT |

The `injection` rule defaults to LOG_ONLY because legitimate prompts
about prompt engineering / red-teaming / safety research would
otherwise trip it. Operators tightening enforcement can flip it to
BLOCK via the per-tenant policy override (Phase 8.2):

```bash
pronaos-cli team set-guardrail-policy <team-id> --set-action injection:block
```

The same column lets ops disable specific rules when redaction
degrades quality on topically-relevant content — see Empirical
claim #3 in [CLAIMS.md](../CLAIMS.md) for the IPv4-in-networking-prompt
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

Live demo (**26.7× speedup** on streaming under provider degradation:
0.33 s skipped-call vs 8.8 s connect-refused timeout) in the
[CLAIMS.md](../CLAIMS.md), Empirical claim #6.

### Distributed mode (Phase 25)

The default per-process breaker is correct on a single container but
costs every replica its own ``failure_threshold`` count before it
converges with the rest. At 5 replicas × threshold 5, that's **25
wasted upstream calls** to a dead provider before the gateway as a
whole skips it.

Opt in to the Redis-backed registry with two env vars:

| Variable | Default | What it does |
| --- | --- | --- |
| `PRONAOS_CIRCUIT_BREAKER_DISTRIBUTED` | `false` | Set `true` to enable Redis-backed breaker state. |
| `PRONAOS_REDIS_URL` | unset | Required when distributed is enabled. The breaker reuses the same Redis the rate limiter and L1 cache already speak to. |

State lives under per-provider hash keys (`pronaos:circuit:{provider}`)
with fields `state` / `consecutive_failures` / `opened_at` / `trip_count`.
Every state transition is an atomic Lua script — concurrent failures
from different replicas can't race past each other on the failure
counter.

**Fail-open invariant kept.** A Redis outage during a breaker call
returns permissive defaults (`allow_request → True`, `record_* →
no-op`) and logs a warning. The gateway keeps serving; worst case is
"degrades to no breaker," which is the same posture as a fresh
deployment.

**Sanity-pinged at startup.** When the distributed flag is on, the
gateway tries `redis.ping()` at lifespan setup. A failed ping logs
`circuit.registry.distributed_unavailable` and falls back to the
in-memory registry so the gateway still boots.

Convergence demo (5 replicas × 1 failure each → all trip in 15 ms):
`python scripts/verify_distributed_circuit.py`. Headline empirical
claim #12 in [CLAIMS.md](../CLAIMS.md).

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

Live demo with cost-savings claim in [CLAIMS.md](../CLAIMS.md),
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

## Cost-aware auto-routing (Phase 21)

When a client sends `model="auto"`, the gateway picks a concrete
provider/model from the team's allowlist using the configured routing
strategy. Resolution is pure in-memory — no I/O, sub-millisecond on
the hot path.

**Seven strategies today:**

| Strategy | Score function | When to use |
| --- | --- | --- |
| `cheapest` | `(in_tokens × in_price + out_tokens × out_price)` from per-model pricing × preflight estimate | FinOps default — minimise spend at the floor of acceptable quality |
| `fastest` | `typical_p50_ms` from the per-provider catalog field | Latency-sensitive workloads (live chat, agent loops) |
| `balanced` | Normalised cost + normalised latency on a 0..1 scale, summed across the pool | Mixed workloads where neither extreme dominates |
| `quality-aware-cheapest` (Phase 24) | Two-stage: drop models whose stored eval score < `team.quality_threshold`, then `cheapest` over what remains. Falls back to plain `cheapest` if no eval scores are stored. | Workloads where you've run eval and have **measured** quality data per model |
| `tool-use-aware-cheapest` (Phase 46) | Same two-stage shape using `team.tool_use_scores` + `team.tool_use_threshold`; filter only fires on requests that carry tools — tool-less requests fall back to `cheapest` | Agentic workloads where tool accuracy varies significantly across models |
| `prompt-cache-aware-cheapest` (Phase 47) | Discounts each candidate's input price by its runtime-observed prompt-cache hit rate (Anthropic 0.10×, OpenAI 0.50×, others 1.0×) before scoring; observations live in Redis via `PromptCacheObserver` | Long-document / repeated-system-prompt workloads where Anthropic or OpenAI prompt-cache is active |
| `reasoning-aware-cheapest` (Phase 57) | Multiplies each candidate's output price by `1 + observed_reasoning_ratio`; optional `team.reasoning_aware_max_ratio` cap excludes reasoning-heavy models from the pool | Workloads that want to avoid runaway thinking-token spend on reasoning-capable models |

**Eligibility filter (runs before scoring):** drops candidates that
can't satisfy the request's capability needs — `requires_tools`,
`requires_vision`, `requires_streaming`, plus a 90 %-of-`max_context`
margin to leave room for the output. Capability data lives in
`providers/catalog.py` per (provider, model).

**Per-team config:** `Team.routing_strategy` column. `NULL` = falls
back to `cheapest`. Set via CLI or admin API:

```bash
pronaos-cli team set-routing-strategy <team-id> --strategy cheapest
pronaos-cli team set-routing-strategy <team-id> --strategy fastest
pronaos-cli team set-routing-strategy <team-id> --strategy balanced

# Admin API:
PUT /v1/admin/team/{team_id}/routing-strategy
GET /v1/admin/team/{team_id}/routing-strategy
```

**Response headers** the gateway stamps on auto-routed responses:

- `X-Pronaos-Routed-Model: <provider/model>` — the concrete model the
  scorer picked (the rest of the pipeline used this, not `auto`)
- `X-Pronaos-Routing-Strategy: <cheapest|fastest|balanced|quality-aware-cheapest|tool-use-aware-cheapest|prompt-cache-aware-cheapest|reasoning-aware-cheapest>`
  — which strategy produced the pick
- `X-Pronaos-Quality-Score: <0.000-1.000>` (Phase 24) — the stored
  eval score for the selected model, when one is on record. Absent
  when the team has no `quality_scores` entry for the picked model.

**Metric:** `pronaos_routing_decisions_total{strategy, selected_model}`
— bounded cardinality (catalog × strategies ≈ 75 series max). Useful
for "what model does `cheapest` pick most often on real traffic?"

**Failure modes:**
- `auto` + empty allowlist or no model satisfies capability →
  HTTP 422 `no_eligible_model` (4xx — client's allowlist too narrow,
  not a server problem).
- `auto` for a request that has tools but no tool-supporting model
  in the allowlist → same 422.

Live demo + empirical claim #8 (cost-aware routing cuts spend ~95 %
at zero quality cost on the basic golden set) in [CLAIMS.md](../CLAIMS.md). Reproduce with `python scripts/eval_cost_routing.py`.

## ML PII detection — Presidio (Phase 22)

Microsoft Presidio runs alongside the regex detectors when enabled.
It wraps **spaCy NER + a pluggable recognizer pipeline** that catches
the long-tail PII the regex layer misses:

| Catches | Example regex misses |
| --- | --- |
| `presidio.PERSON` | "John Smith", "Dr. Maria Gonzalez" |
| `presidio.LOCATION` | "San Francisco", "Germany" |
| `presidio.DATE_TIME` | "January 3rd, 1985", "next Friday at 3pm" |
| `presidio.EMAIL_ADDRESS` | (same as regex; both fire on a control case) |
| `presidio.PHONE_NUMBER` | "+44 20 7946 0958" (UK format regex misses) |
| `presidio.US_SSN` / `IP_ADDRESS` / `CREDIT_CARD` | Overlap with regex |
| ... 50 + entity types | See Presidio's recognizer registry |

**Opt-in, not default:** spaCy + presidio-analyzer pull in ~600 MB
of model + library state. Set `PRONAOS_PRESIDIO_ENABLED=true` to
register the detector. Without it the regex layer alone runs.

**Lazy load:** the spaCy model loads on the first scan (~1-2 s,
~250 MB RAM). Subsequent scans are microseconds. Gateway startup is
unaffected.

**Per-team toggle:**

```bash
# Disable Presidio for one team (regex still runs):
pronaos-cli team set-guardrail-policy <team-id> \
    --policy-json '{"presidio": {"enabled": false}}'

# Or via the CLI's --disable shortcut:
pronaos-cli team set-guardrail-policy <team-id> --disable presidio
```

The shorthand `{"presidio": {"enabled": false}}` in the team's
`guardrail_policy` JSON is resolved to `disabled_rules += {"presidio"}`
at request time, skipping the whole detector. Per-entity disabling
(e.g. only skip `presidio.DATE_TIME`) is a Phase 22.1 follow-up;
today the toggle is whole-engine.

**Fail-open semantics:** ImportError, spaCy model-load failure, or
runtime exception inside `analyze()` all return empty hits and log
a warning. The gateway must keep serving even if Presidio is broken.
The regex detectors remain active independently.

**Metric labels:** each Presidio hit reports under its entity name
(`presidio.PERSON`, `presidio.LOCATION`, etc.) so the existing
`pronaos_guardrail_hits_total{rule, action, direction}` counter
already breaks out per-entity without schema changes. A panel
filtered on `rule=~"presidio\\..*"` shows ML-detection volume.

**Tuning knobs (env vars):**

| Variable | Default | What it does |
| --- | --- | --- |
| `PRONAOS_PRESIDIO_ENABLED` | `false` | Register the detector at startup. Required for any Presidio scanning. |
| `PRONAOS_PRESIDIO_MIN_SCORE` | `0.5` | Confidence floor (0..1). Lower = more recall, more false positives. Presidio's own default is 0.5. |

Live demo + empirical claim #9 (9 PII cases caught only by ML, 0
by regex on a curated 12-case probe set) in [CLAIMS.md](../CLAIMS.md).
Reproduce with `python scripts/eval_pii_coverage.py`.

## Multi-judge eval (Phase 23)

LLM-as-judge has a known weakness: a single judge is one opinion.
The fix is to run *two different* judges on the same response and
report how often they agree. Pronaos's `MultiJudgeRunner` does this:

- Calls the candidate model **once** per case (one provider hit,
  one bill).
- Hands the response to **N judges concurrently** (asyncio.gather —
  independent judges, no shared state to race).
- Computes pairwise agreement metrics.

**Three agreement metrics** per pair of judges:

- **mean_abs_delta** — average `|score_a - score_b|` across cases.
  Lower is better. The intuition: how far apart do the judges sit
  on average?
- **within_epsilon_rate** — fraction of cases where `|Δ| ≤ ε`
  (default `ε=0.1`). The *headline*: how often are judges
  effectively agreeing?
- **cohens_kappa** — chance-corrected binary agreement on
  `pass/fail` at the `pass_threshold`. Returns `0.0` (safe
  sentinel) when marginals are degenerate — i.e. one judge passed
  or failed everything. Useful when scores are bimodal.

**CLI usage** — pass a comma-separated `--judge-model`:

```bash
pronaos-cli eval run \
    -g tests/eval/data/basic.yaml \
    -c groq/llama-3.1-8b-instant \
    -j "groq/llama-3.3-70b-versatile,groq/meta-llama/llama-4-scout-17b-16e-instruct" \
    -k pn_live_... --base-url http://127.0.0.1:8080 \
    -o eval-results/multi-judge.json
```

Single judge (no comma) routes to the existing single-judge runner
— backwards-compatible.

**Output shape** — JSON with `per_judge` stats + `pairs` agreement
+ full per-case verdicts (one row per case × judge). Safe to diff
across runs as a CI gate.

Live demo + empirical claim #10 (two Groq judges agreed on 8/8
basic-suite cases: mean |Δ|=0.000, within-ε=100 %) in [CLAIMS.md](../CLAIMS.md).

## Quality-aware routing (Phase 24)

Closes the loop between the eval harness and the cost-aware router.
The team's eval results — per-model quality scores from
`pronaos-cli eval run` — are stored on the team and used as a filter
when `routing_strategy="quality-aware-cheapest"`.

**Workflow**

1. Run an eval against each candidate model, saving JSON output:

   ```bash
   pronaos-cli eval run -g basic.yaml -c groq/llama-3.1-8b-instant \
       -j groq/llama-3.3-70b-versatile -k <key> -o eval-results/8b.json

   pronaos-cli eval run -g basic.yaml -c groq/llama-3.3-70b-versatile \
       -j groq/llama-3.3-70b-versatile -k <key> -o eval-results/70b.json
   ```

   Multi-judge (`-j a,b`) JSON is also accepted; scores are averaged
   across judges per model.

2. Persist scores onto the team:

   ```bash
   pronaos-cli eval store-scores --team <team-id> --from eval-results/8b.json
   pronaos-cli eval store-scores --team <team-id> --from eval-results/70b.json

   # Inspect:
   pronaos-cli eval store-scores --team <team-id> --from /dev/null --show

   # Clear (back to no eval data):
   pronaos-cli eval store-scores --team <team-id> --from /dev/null --clear
   ```

3. Switch the team's routing strategy:

   ```bash
   pronaos-cli team set-routing-strategy <team-id> \
       --strategy quality-aware-cheapest
   ```

**Selection algorithm**

Two-stage filter inside the scorer:

1. **Eligibility** — capability filter (tools / vision / streaming /
   context). Same as the other strategies.
2. **Quality** — drop candidates whose stored score is below
   `team.quality_threshold` (default `0.7` when NULL). Models with
   **no** entry in `quality_scores` are kept (no evidence either
   way; don't penalise unevaluated models).
3. **Cost** — pick the cheapest of what remains using
   `CostScorer` × the preflight token estimate.

**Failure modes**

- **No eval data on file** — `quality_scores` is NULL or empty. The
  strategy degrades to plain `cheapest` over the capability-
  eligible pool. Safe default for teams that switched the strategy
  before running eval.
- **No model clears the threshold** — every evaluated candidate is
  below `quality_threshold`. The gateway returns
  `HTTP 422 no_eligible_model` with a message asking the operator
  to lower the threshold or widen the allowlist.

**Schema (Phase 24, migration 0010)**

| Column | Type | Meaning |
| --- | --- | --- |
| `teams.quality_threshold` | Float, nullable | Score floor 0..1 for the quality filter. NULL → uses `DEFAULT_QUALITY_THRESHOLD = 0.7`. |
| `teams.quality_scores` | JSON, nullable | `{fqmn: {"score": float, "n_samples": int, "source_eval_id": str, "ts": iso8601}}`. Populated by `eval store-scores`. |

Live demo + empirical claim #11 (auto-routing upgrades from
8B → 70B when stored eval data shows 8B under-performs) in
[`CLAIMS.md`](../CLAIMS.md).

## Request hedging (Phase 27)

Tail-latency reduction by speculative parallel start. When the team's
`hedge_delay_ms` is set, the failover executor waits that long for the
primary to return; if it hasn't, an identical call is started against
the next chain provider. `asyncio.wait FIRST_COMPLETED` selects the
winner; the loser is cancelled (`httpx` stream closes propagate). The
recognised "tail at scale" technique (Dean & Barroso, CACM 2013).

**Per-team policy columns** (Phase 27 migration 0012):

| Column | Type | Meaning |
| --- | --- | --- |
| `teams.hedge_delay_ms` | Float, nullable | Wait this long for primary; NULL/0.0 disables hedging |
| `teams.hedge_max_count` | Integer, nullable | Cap on hedges per request; NULL = 1 (default) |

**CLI + admin API:**

```bash
pronaos-cli team set-hedge-policy <team-id> --delay-ms 150
pronaos-cli team set-hedge-policy <team-id> --delay-ms 200 --max-count 2
pronaos-cli team set-hedge-policy <team-id> --clear
pronaos-cli team set-hedge-policy <team-id> --show

# Admin API:
PUT /v1/admin/team/{team_id}/hedge-policy
GET /v1/admin/team/{team_id}/hedge-policy
```

**Three metrics** for full observability:

- `pronaos_hedge_triggered_total{primary, hedge}` — times the hedge
  fired (the primary did not return inside `hedge_delay_ms`).
- `pronaos_hedge_wins_total{winner_provider, role}` — race outcomes.
  `role` is `"primary"` when the original beat its hedge, `"hedge"`
  when the speculative call won. The mix tells you whether the
  delay is set right — heavy `primary` wins mean the delay is too
  short; heavy `hedge` wins mean it's well-tuned.
- `pronaos_hedge_cancelled_total{cancelled_provider}` — the loser of
  each race, counted as a wasted upstream attempt. Multiply by mean
  cost-per-call for honest overhead reporting.

**Response headers** the gateway stamps on hedged responses:

- `X-Pronaos-Hedged: true` — a hedge was actually started (the primary
  did not return inside the delay).
- `X-Pronaos-Hedge-Winner: <primary|hedge>` — who won the race.
- `X-Pronaos-Hedge-Provider: <provider/model>` — which provider the
  speculative call went to.

**Honesty notes:**
- Hedging only helps when slow events are *uncorrelated* across
  providers. A shared upstream dependency or a regional outage means
  hedge slow co-occurs with primary slow; cost overhead is real, p99
  reduction is zero. The demo script falsifies this case explicitly
  by exit-code on insufficient improvement.
- Streaming-aware: the same race resolves at "headers received,"
  matching where the existing failover layer commits. A hedge that
  wins the race owns the stream from chunk 0; the loser's connection
  is closed before any tokens leak through.
- Breaker-aware: a hedge candidate whose breaker is OPEN is skipped.
  Hedging cannot "wake up" a known-bad provider — that's the
  breaker's job, not the hedge layer's.

Live demo + empirical claim #14 (p99 cut from 813 ms to 235 ms at +6%
upstream-call overhead on a 7%-slow-tail workload) in [CLAIMS.md](../CLAIMS.md). Reproduce with `python scripts/verify_hedging_latency.py`.

## OIDC / SSO admin auth (Phase 26)

A second Bearer path runs alongside the API-key path. Inbound
tokens are dispatched by shape:

- **Underscore-separated** (`pn_live_...`) → existing API-key
  verify (argon2 hash check on the database row keyed by prefix).
- **Dot-separated** (`<header>.<payload>.<signature>`) → OIDC
  verifier (JWKS signature check + standard claim validation +
  tenant lookup by `sub` claim).

The shapes never collide on the wire, so the auth middleware
dispatches structurally — no per-route flag, no operator config.

**Opt-in via env vars:**

| Variable | Default | What it does |
| --- | --- | --- |
| `PRONAOS_OIDC_ISSUER` | unset | OIDC issuer URL (e.g. `https://keycloak.example.com/realms/pronaos`). Setting this enables the OIDC path. |
| `PRONAOS_OIDC_AUDIENCE` | unset | Expected `aud` claim. When set, JWTs without a matching `aud` are rejected (belt-and-braces against cross-service token reuse). When unset, the audience check is skipped. |
| `PRONAOS_OIDC_JWKS_URL` | discovery default | Override for the JWKS endpoint. Default is `{issuer}/protocol/openid-connect/certs` (Keycloak's layout); set explicitly for IdPs whose layout differs (Auth0: `/.well-known/jwks.json`, Google: `/oauth2/v3/certs`). |

**Per-tenant SSO mapping:**

The `tenants.oidc_subject` column carries the IdP's `sub` claim
(or a deterministic equivalent like `preferred_username` or `oid`)
for the tenant's admin. When a verified JWT arrives, the gateway
queries `WHERE oidc_subject = <sub>` and grants `admin:usage` for
that tenant. NULL on the column means "no OIDC admin for this
tenant" — only the API-key path works.

**JWKS caching:** PyJWKClient caches the JWKS in-memory with a
5-minute TTL. Routine key rotations propagate within that
window; emergency rotations require a gateway restart (or the
operator can set a shorter TTL in a future config).

**Signing algorithms accepted:** `RS256` / `RS384` / `RS512` /
`ES256` / `ES384` / `PS256`. Symmetric (`HS256`) is deliberately
excluded — JWKS verification only makes sense with asymmetric
signatures.

**Fail-closed semantics.** Any verification error — bad signature,
expired token, wrong issuer, JWKS unreachable, unknown `sub` —
surfaces as 401 with the same error text as a bad API key (no
enumeration leak). Operator-side observability lives in the
structured log: `oidc.verifier.enabled` at startup, plus
warnings like `oidc.jwks_lookup_failed` on runtime issues.

Live demo + empirical claim #13 (real RSA-2048 keypair + real
JWKS served over HTTP + JWT → 200 OK on `/v1/admin/usage`) in
[`CLAIMS.md`](../CLAIMS.md). Reproduce with `python scripts/verify_oidc_live.py`.

## Agent-turn budget gates (Phase 30)

Monthly budgets protect against long-term spend; they don't protect
against a single misbehaving agent loop that burns a month of budget
in fifteen minutes. The agent-turn gate closes that gap with a
**per-execution** budget enforced via Redis.

**How clients opt in.** Any request carrying `X-Pronaos-Agent-Turn-ID`
participates in the per-turn accumulator. Calls without the header
bypass the gate (zero behavioural change for non-agent traffic).

**How teams opt in.** Three nullable columns on `teams`:

- `agent_turn_budget_tokens` — total token budget for one turn
- `agent_turn_budget_cost_hcents` — total cost budget (hundredths of a cent)
- `agent_turn_ttl_seconds` — accumulator key TTL (default 3600 s)

Both budget columns NULL = no gate for that team.

**CLI:**

```bash
pronaos-cli team set-agent-budget <team-id> --tokens 5000 --cost-hcents 200
pronaos-cli team set-agent-budget <team-id> --ttl 1800
pronaos-cli team set-agent-budget <team-id> --clear
pronaos-cli team set-agent-budget <team-id> --show
```

**Storage shape (Redis Hash):**

```
KEY:    pronaos:agentturn:{team_id}:{turn_id}
FIELDS: tokens (int), cost_hcents (int), calls (int)
TTL:    agent_turn_ttl_seconds (default 3600)
```

**Pre-call check** reads the hash + the preflight estimate (claim #7)
and decides allow/deny:

```python
allowed = (used_tokens + estimate_tokens <= budget_tokens) AND
          (used_cost_hcents + estimate_cost_hcents <= budget_cost_hcents)
```

Strictly `<=` — we deny only on **STRICTLY >** budget, so a perfectly
sized last call lands cleanly.

**Post-call record** HINCRBYs actual tokens + cost atomically.

**Response headers** the gateway stamps on every chat call (success and
denial):

- `X-Pronaos-Agent-Turn-ID` — echoes the client's turn-id
- `X-Pronaos-Agent-Turn-Used-Tokens` — running token total
- `X-Pronaos-Agent-Turn-Used-Cost-Hcents` — running cost total
- `X-Pronaos-Agent-Turn-Calls` — calls counted in this turn
- `X-Pronaos-Agent-Turn-Remaining-Tokens` — `budget - used` (clamped ≥ 0)
- `X-Pronaos-Agent-Turn-Remaining-Cost-Hcents` — `budget - used` (clamped ≥ 0)

**Metric:** `pronaos_agent_turn_denials_total{reason}` — `reason` is
`agent_turn_token_budget_exhausted` or `agent_turn_cost_budget_exhausted`.
A flat counter is the dashboard signal that no agent is hitting the cap;
a sudden ramp means a runaway loop is being correctly throttled.

**Denial response:** HTTP 429 with body
`{"detail": {"type": "agent_turn_*_budget_exhausted", "message": "..."}}`
plus all the headers above. Clients can inspect the headers without
re-parsing the body.

**Fail-open.** Redis outage = tracker returns `allowed=True`. The
monthly token + cost budgets still enforce, so degradation cannot
unlock unbounded spend. Operator visibility: `agent_turn.tracker.redis_unavailable`
warning at startup, plus the metric going flat in the dashboard.

Live demo + empirical claim #17 (budget=300, 5 calls allowed
cumulatively reach 301 tokens, call #6 denied with `remaining_tokens=0`,
fresh turn-id accepted immediately) in [`CLAIMS.md`](../CLAIMS.md).
Reproduce with `python scripts/verify_agent_turn_budget.py`.

## Embeddings endpoint (Phase 31)

`POST /v1/embeddings` is a full first-class endpoint with the same
pipeline as chat: auth → allowlist → preflight → ingress guardrails →
cache → provider → cache write → audit + usage record. The killer
feature: identical inputs return byte-identical vectors from cache with
zero upstream cost.

**Supported provider shapes:**

| Shape | Providers | Wire format |
| --- | --- | --- |
| `openai` (default) | `openai/`, `mistral/`, `openrouter/`, `together/` | `{"model": …, "input": str \| list[str], "dimensions": int?}` |
| `cohere` | `cohere/` | `{"model": …, "texts": list[str], "input_type": "search_document"}` |
| `voyage` | `voyage/` | `{"model": …, "input": str \| list[str], "input_type": "query"?}` |
| local | `local/` | in-process sentence-transformers, no HTTP |

**Cache key:**

```
KEY:     cache:exact:{tenant_id}:{model}:{sha256({"type":"embedding","input":[...],"dimensions":N,"input_type":...,"encoding_format":"float"})}
VALUE:   the full OpenAI-shape response body (data + usage + model)
```

The cache key is the same backend that powers chat caching — no new
Redis namespace, no new infrastructure. Multi-replica deployments
share cache hits via the existing Redis backend.

**Metrics:**

- `pronaos_embedding_requests_total{provider, model, status}` —
  every call, labelled by status (success | error).
- `pronaos_embedding_request_duration_seconds{provider, model}` —
  histogram of provider-call duration (excludes cache-hit fast path,
  which is sub-millisecond).
- `pronaos_embedding_tokens_total{provider, model}` — running total
  of input tokens consumed (no completion tokens for embeddings).
- `pronaos_embedding_cache_hits_total{model}` — calls served from
  cache. Divide by `pronaos_embedding_requests_total` for hit-rate.

Per-call cost lands in `pronaos_provider_cost_hcents_total{provider, model}`
— the same counter chat uses — so FinOps dashboards can sum chat +
embedding spend in one query.

**Response headers** stamped on every call (success or error):

- `X-Pronaos-Cache` — `hit:exact` | `miss`
- `X-Pronaos-Provider` — provider key
- `X-Pronaos-Cost-Hcents` — this call's cost in hundredths of a cent
- `X-Pronaos-Preflight-Estimate` — the preflight token estimate

**Failure modes & posture:**

- **Provider not configured** (no API key set): 503
  `provider_not_configured` — same error shape as chat.
- **Model not in team allowlist**: 403 `model_not_allowed`.
- **Preflight over budget**: 429 `monthly_token_budget_exhausted`
  with `X-Pronaos-Preflight-Estimate` header.
- **PII detected (per team policy = BLOCK)**: 400 `guardrail_blocked`
  before the upstream call.
- **Upstream auth / rate-limit / 5xx**: 502 with `upstream_*` reason
  codes mirroring chat's error vocabulary.

**Honesty note.** The local sentence-transformers backend is bundled
for reproducibility — it lets contributors run the live demo without
paying for an upstream. For production RAG workloads the cost story
is dramatic against OpenAI/Cohere/Voyage; against local, the cache
saves CPU cycles but is largely masked by SQLite + audit + usage
write overhead at the gateway level. Cache *correctness* (byte-identical
vectors on cache hit, zero upstream calls) holds in either configuration.

Live demo + empirical claim #18 in [`CLAIMS.md`](../CLAIMS.md).
Reproduce with `python scripts/verify_embeddings.py`.

## Rerank endpoint (Phase 32)

`POST /v1/rerank` completes the RAG triad. Pipeline reuse mirrors
`/v1/embeddings`; the cache is the killer feature: identical search
parameters return byte-identical relevance scores at zero upstream cost.

**Supported provider shapes:**

| Shape | Providers | Wire format | Billing |
| --- | --- | --- | --- |
| `cohere` | `cohere/` | `{"model":…, "query":…, "documents":[…], "top_n":N, "return_documents":bool}` | per call ("search unit"), one per call up to 100 docs |
| `voyage` | `voyage/` | `{"model":…, "query":…, "documents":[…], "top_k":N, "return_documents":bool}` | per token (sum of query + documents) |

The public endpoint uses Cohere's spelling (`top_n`). The Voyage
adapter translates internally to `top_k`. No third "openai" shape
exists today — OpenAI doesn't ship a rerank API.

**Cache key:**

```
KEY:    cache:exact:{tenant_id}:{model}:{sha256({"type":"rerank","query":...,"documents":[...],"top_n":N,"return_documents":bool})}
VALUE:  the full Cohere-like response body (data + usage + model)
```

**Metrics:**

- `pronaos_rerank_requests_total{provider, model, status}` — every call,
  labelled by status (success | error).
- `pronaos_rerank_request_duration_seconds{provider, model}` —
  histogram of provider-call duration (excludes cache-hit fast path).
- `pronaos_rerank_cache_hits_total{model}` — calls served from cache.

Per-call cost lands in `pronaos_provider_cost_hcents_total{provider, model}`
alongside chat + embedding spend — FinOps dashboards sum across all three
endpoints in one query.

**Response headers** stamped on every call:

- `X-Pronaos-Cache` — `hit:exact` | `miss`
- `X-Pronaos-Provider` — provider key (`cohere` | `voyage`)
- `X-Pronaos-Cost-Hcents` — this call's cost (cache hits don't stamp;
  the original audit row is canonical)
- `X-Pronaos-Preflight-Estimate` — preflight token estimate over
  query + all documents

**Honesty notes:**

- The cache wins are workload-dependent. RAG re-indexing (same documents,
  repeated runs) hits ~100% cache rate; novel-query conversational RAG
  hits ~0%. The narrative is honest about which workloads benefit.
- Cohere's per-call billing means a 5-document rerank costs the same as
  a 100-document rerank. Voyage's per-token billing scales linearly.
  The cache eliminates both.
- No local rerank backend bundled (unlike embeddings). Cross-encoder
  reranking is compute-heavy enough we don't ship a local fallback;
  contributors can wire BGE reranker through a custom catalog entry.

Live demo + empirical claim #19 in [`CLAIMS.md`](../CLAIMS.md).
Reproduce with `python scripts/verify_rerank.py`.

## Singleflight dedup (Phase 33)

Concurrent identical requests on a cold cache fire N independent
upstream calls; with singleflight, only the first becomes the leader
(does the upstream call + cache write), and the rest become followers
awaiting the leader's future. Standard Go-style semantics — followers
share the leader's result OR its exception.

**Where it applies:** `/v1/embeddings`, `/v1/rerank`. Chat is a
follow-up (streaming + hedging + A/B paths warrant their own phase).

**Tenant-isolated keys.** The singleflight key shape is
`sha256({tenant_id, model, cache_payload})` — two tenants embedding
the same text do NOT share a leader.

**Failure semantics.** When the leader's `fn` raises:

```python
future.set_exception(e)  # propagates to all followers
self._in_flight.pop(key, None)  # next arrival fresh
```

All N callers see the same exception. The next arrival AFTER the
exception propagated takes a fresh leader slot — transient failures
don't lock followers out forever.

**Metric:** `pronaos_singleflight_followers_total{endpoint}` where
`endpoint ∈ {embedding, rerank}`. Each follower represents one saved
upstream invocation. Compute the dedup rate as:

```promql
sum(rate(pronaos_singleflight_followers_total[5m]))
/
sum(rate(pronaos_embedding_requests_total[5m]) + rate(pronaos_rerank_requests_total[5m]))
```

A high ratio means bursty identical-input workload; flat counter
means no concurrent duplicates (singleflight idle but harmless).

**Response header on followers:** `X-Pronaos-Singleflight: follower`.
Lets clients audit which calls were deduplicated without parsing logs.

**Usage record accounting.** Followers get a row with `cost_hcents=0`
and `prompt_tokens=0` — they didn't trigger an upstream call. The
leader carries the full cost. This keeps `usage_records` faithful to
actual upstream spend (a 50-burst that triggered 1 upstream call
shows 1 expensive row + 49 zero-cost rows).

**Cross-replica mode (Phase 36).** Opt-in via
`PRONAOS_SINGLEFLIGHT_DISTRIBUTED=true` (requires `PRONAOS_REDIS_URL`).
The factory in `main.py` swaps in `RedisSingleflightRegistry` at
startup; handler code doesn't change. The Redis registry uses atomic
`SET NX` on `pronaos:singleflight:{key}` plus ~50 ms polling for
followers; same-replica fast path catches concurrent same-process
callers BEFORE hitting Redis. TTL (default 60 s, tunable via
`PRONAOS_SINGLEFLIGHT_TTL_SECONDS`) bounds the absolute wait if the
leader dies mid-call. Leader exceptions propagate cross-replica as
`CrossReplicaLeaderError` carrying the original class name + message.
Empirical claim #23 shows 50 concurrent calls across 5 simulated
replicas collapsing to 1 upstream invocation.

Live demo + empirical claim #20 in [`CLAIMS.md`](../CLAIMS.md).
Reproduce with `python scripts/verify_singleflight.py`.

## Anthropic prompt-cache FinOps (Phase 34)

Anthropic's prompt caching (`cache_control: {"type":"ephemeral"}`
blocks) gives ~90% cost reduction on cached prefixes. Pronaos surfaces
the FinOps win end-to-end.

**What the adapter extracts.** Anthropic's response usage block
carries two new fields when the client used `cache_control`:

- `cache_creation_input_tokens` — tokens billed at the cache-write
  rate (1.25x regular input)
- `cache_read_input_tokens` — tokens served from cache (0.10x —
  the headline 90% discount)

Both are pulled from streaming (`message_start` event) and
non-streaming (response root) paths.

**Weighted cost math.** `AnthropicProvider.cost_cents` applies the
published ratios via integer scaling:

```
input_cost      = prompt_tokens * input_rate / 1M
cache_write_cost = cache_creation_tokens * input_rate * 1.25 / 1M
cache_read_cost  = cache_read_tokens * input_rate * 0.10 / 1M
output_cost     = completion_tokens * output_rate / 1M
total = input_cost + cache_write_cost + cache_read_cost + output_cost
```

Validated by 4 dedicated unit tests asserting cache reads cost
exactly 10% and cache writes cost exactly 125% of the regular input
rate. If Anthropic changes the ratios, those tests fail in one place.

**Response headers** stamped on every chat completion that returned
non-zero cache stats:

- `X-Pronaos-Prompt-Cache-Read-Tokens` — cache hits this call
- `X-Pronaos-Prompt-Cache-Write-Tokens` — cache creation this call
- `X-Pronaos-Prompt-Cache-Saved-Hcents` — counterfactual savings
  (what the call WOULD have cost without caching, minus what we
  actually paid)

When the client didn't use `cache_control`, all three headers are
omitted (clean response shape for the common case).

**Response body's `pronaos` block** extends with the same numbers:

```json
"pronaos": {
  "provider": "anthropic",
  "cost_hcents": 2025,
  "cache_read_tokens": 10000,
  "cache_creation_tokens": 0,
  "cache_saved_hcents": 13500
}
```

**`usage_records.cost_hcents` reflects the post-discount cost.** The
team is billed at the discounted rate; FinOps queries (`pronaos-cli
team chargeback`, `GET /v1/admin/usage`) automatically reflect the
savings. No double-charging, no manual reconciliation.

**Metric:** `pronaos_prompt_cache_tokens_total{provider, model, type}`
where `type ∈ {read, write}`. Compute the cache-hit ratio:

```promql
sum(rate(pronaos_prompt_cache_tokens_total{type="read"}[5m]))
/
sum(rate(pronaos_provider_tokens_total{direction="prompt"}[5m]))
```

A growing ratio means more of your input tokens are being served from
cache — the healthier the trend, the better the FinOps story.

**Honesty notes:**

- OpenAI-compat providers don't expose these fields (their discounts
  are applied at the provider tier, not surfaced as separate token
  counters). The cache headers don't stamp on OpenAI/Groq/Together
  calls — by design.
- Anthropic's 5-minute cache TTL is upstream's concern. Pronaos
  doesn't track expiry; if a cached prefix lapses, the next call
  reports `cache_read_tokens=0` again and the team pays full input rate.
- Streaming + cache_control is supported. The adapter pulls cache
  stats from the `message_start` SSE event.

Live demo + empirical claim #21 in [`CLAIMS.md`](../CLAIMS.md).
Reproduce with `python scripts/verify_anthropic_cache.py`.

## OpenAI prompt-cache FinOps (Phase 35)

OpenAI auto-caches prompt prefixes ≥1024 tokens on supported models
(gpt-4o, gpt-4o-mini, o1-preview, o1-mini, gpt-4-turbo) since late
2024. Caching is **automatic** — no client opt-in, no `cache_control`
blocks. Cached tokens are billed at **0.5x** the regular input rate
(50% discount). OpenAI does NOT charge a cache-write premium
(unlike Anthropic's 1.25x).

**Adapter extraction.** OpenAI's response usage block carries
`prompt_tokens_details.cached_tokens` on both streaming + non-streaming
responses. Pronaos's OpenAI-compat adapter extracts the field and
**normalises `prompt_tokens` to the non-cached portion** (subtracts
the cached count). This is critical because OpenAI's `prompt_tokens`
field includes cached tokens — without normalisation the chat handler
would double-count.

```python
# Non-streaming (in OpenAICompatibleProvider._chunk_from_response):
details = usage.get("prompt_tokens_details")
cache_read_tokens = (
    int(details.get("cached_tokens") or 0)
    if isinstance(details, dict) else 0
)
raw_prompt_tokens = usage.get("prompt_tokens")
non_cached = max(0, int(raw_prompt_tokens) - cache_read_tokens)
return ChatCompletionChunk(
    prompt_tokens=non_cached,       # normalised
    cache_read_tokens=cache_read_tokens,
    cache_creation_tokens=0,        # OpenAI doesn't expose cache writes
    ...
)
```

**Weighted cost math** in `cost_cents`:

```
input_cost      = non_cached_prompt * input_rate / 1M
cache_read_cost = cache_read_tokens * input_rate / 2 / 1M   # 0.5x
output_cost     = completion_tokens * output_rate / 1M
total = input_cost + cache_read_cost + output_cost
```

**Same response headers + metadata as Anthropic.** Because the adapter
normalises `prompt_tokens`, the existing Phase 34 chat-handler code
works as-is for OpenAI. Clients get the same `X-Pronaos-Prompt-Cache-*`
headers + `response.pronaos.cache_*` body fields regardless of which
provider served the call.

**Other OpenAI-compat providers don't expose this field.** Groq,
DeepSeek, Together, Fireworks, Perplexity, xAI, Cerebras, Mistral,
OpenRouter, Ollama — none ship prompt-cache attribution today.
Extraction falls through to 0, cost math reduces to the legacy
input+output sum, no behavioural change. Existing tests still pass.

**Metric**: same as Phase 34 — `pronaos_prompt_cache_tokens_total{provider, model, type}`
with `type=read`. (OpenAI never increments `type=write` since it
doesn't surface cache writes.)

**Honesty notes:**

- OpenAI's cache TTL is opportunistic. Same prompt repeated within
  the cache window hits; outside it doesn't. Live verification can
  occasionally see `cached_tokens=0` on call 2 if OpenAI's cache
  state evicted between calls.
- Pronaos trusts OpenAI's reported `cached_tokens` count — we don't
  independently tokenise. If OpenAI under-reports, Pronaos
  under-credits the savings.
- Below 1024-token prompts will not cache (OpenAI's minimum). The
  live-verify script uses a ~2000-token system prompt to stay above
  the threshold.

Live demo + empirical claim #22 in [`CLAIMS.md`](../CLAIMS.md).
Reproduce with `python scripts/verify_openai_cache.py`.

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
