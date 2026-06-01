# Pronaos — Architecture

High-level architecture of what's built and running today. For deeper
operational detail see [`observability/README.md`](observability/README.md);
for empirical claims about behaviour see [`CLAIMS.md`](CLAIMS.md).

## Goals

1. **Single governed hop** for every LLM call an organization makes.
2. **Provider-agnostic** — same OpenAI-shape API regardless of upstream
   (14 providers wired today: Anthropic native, AWS Bedrock native with SigV4, Google Vertex AI native with GCP JWT auth, and 11 via OpenAI-compat; bidirectional translation for Anthropic).
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
                   │  auth ─► [model="auto" → cost-aware route] ─► allowlist ─►   │
                   │  preflight ─► guardrails-in (regex + Presidio ML) ─►         │
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
- Scopes today: `chat:write`, `admin:usage`, `admin:identity`.
- **OIDC/SSO** (Phase 26): a parallel Bearer-JWT path runs alongside
  the API-key path. JWTs are verified against the IdP's JWKS
  (signature + `iss`/`aud`/`exp` checks via PyJWT); the `sub` claim
  resolves to a tenant via `tenants.oidc_subject`, granting the
  `admin:usage` scope. Wire-shape dispatch (dot-separated JWT vs
  underscore-separated API key) means the two paths never collide.
  Opt-in via `PRONAOS_OIDC_ISSUER`.

### 2. Cost-aware auto-routing (`model="auto"`)

- Clients can send the sentinel `model="auto"` to defer model selection
  to the gateway. Resolution is sub-millisecond, pure in-process —
  no I/O.
- Pipeline: preflight estimator → eligibility filter (capabilities:
  tools, vision, streaming, max_context — sourced from a per-model
  matrix in `providers/catalog.py`) → score with the team's
  `RoutingStrategy` → deterministic tiebreak by fqmn.
- Seven strategies today: `cheapest` (minimise expected cost from
  the per-model pricing × estimated tokens), `fastest` (minimise
  `typical_p50_ms` from the catalog), `balanced` (normalised cost
  + normalised latency, summed), `quality-aware-cheapest`
  (Phase 24: two-stage filter — drop models whose stored eval score
  is below `team.quality_threshold`, then pick cheapest of what
  remains; falls back to plain `cheapest` if no scores are stored),
  `tool-use-aware-cheapest` (Phase 46: same shape but reads
  `team.tool_use_scores` + `team.tool_use_threshold`; filter only
  fires when the inbound request actually carries tools — tool-less
  requests degrade to plain `cheapest` so the filter applies
  surgically), `prompt-cache-aware-cheapest` (Phase 47:
  discounts each candidate's nominal input rate by the team's
  runtime-observed cache hit rate × the provider's cache-read
  pricing multiplier — Anthropic 0.10x, OpenAI 0.50x, others 1.0 =
  no-op; observations live in Redis via `PromptCacheObserver`,
  thresholds on the team row; degrades to plain `cheapest` when no
  observation crosses the sample/hit-rate gates), and
  `reasoning-aware-cheapest` (Phase 57: multiplies each candidate's
  nominal output rate by `1 + observed_reasoning_ratio` so a model
  burning 50% of its output on reasoning costs 1.5× its nominal
  output rate; observations live in Redis via `ReasoningObserver`
  (parallel to PromptCacheObserver); optional per-team `max_ratio`
  cap excludes reasoning-heavy models from the pool entirely;
  degrades to plain `cheapest` when no observation crosses the
  sample gate).
- Per-team `routing_strategy` column; `NULL` = falls back to
  `cheapest`. Set via `pronaos-cli team set-routing-strategy` or
  `PUT /v1/admin/team/{id}/routing-strategy`.
- Decision surfaced in response headers: `X-Pronaos-Routed-Model`,
  `X-Pronaos-Routing-Strategy`, plus `X-Pronaos-Quality-Score` when
  the selected model has a stored eval score (Phase 24). Each
  decision ticks `pronaos_routing_decisions_total{strategy,
  selected_model}`.
- The eval harness (Phase 9 / Phase 23 multi-judge) writes per-model
  scores into `team.quality_scores` via the `pronaos-cli eval
  store-scores` CLI. The scorer reads them at request time — no
  separate scheduler, no background job.
- Phase 46 adds a parallel column pair `team.tool_use_scores` +
  `team.tool_use_threshold` written via PUT
  `/v1/admin/team/{id}/tool-use-scores` and read by the
  `tool-use-aware-cheapest` strategy. Same write-once-read-each-request
  model as quality scores, no scheduler. The scores come from
  Phase 45's BFCL-style eval (`scripts/eval_tool_use_accuracy.py`).
- Non-`auto` requests bypass this stage entirely.

### 3. Model allowlist gate

- Per-team `allowed_models` column (JSON list of fnmatch patterns).
- `NULL` = unrestricted (backwards-compat default); `[]` = explicit
  deny-all (paused team without revoking keys).
- Runs after auto-routing (so the resolved model gets checked) but
  before quota / guardrails / cache.
- For auto-routed requests the scorer enforces the allowlist
  internally at candidate-build time; this gate is defence-in-depth.
- Returns `403 model_not_allowed` with the offending model name.

### 4. Pre-flight token estimator + quota gate

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

### 4a. Agent-turn budget gate (Phase 30)

- **Opt-in via header.** Clients running agent loops tag every call in
  one logical execution with the same `X-Pronaos-Agent-Turn-ID` UUID.
  Calls without the header bypass the gate (no behavioral change for
  non-agent traffic).
- **Opt-in per team.** `agent_turn_budget_tokens` and
  `agent_turn_budget_cost_hcents` on the team row are nullable; teams
  that leave both NULL aren't gated.
- **Redis-backed accumulator** at `pronaos:agentturn:{team_id}:{turn_id}`
  (Hash: `tokens`, `cost_hcents`, `calls`). Pre-call `check()` reads
  the running total + the next call's preflight estimate; if the sum
  would exceed the budget, returns HTTP **429** with reason
  `agent_turn_token_budget_exhausted` or `agent_turn_cost_budget_exhausted`.
  Post-call `record()` HINCRBYs the actual usage atomically.
- **Per-execution isolation.** The hash key includes the turn-id, so
  two parallel agent runs from the same team accumulate independently.
  A fresh turn-id immediately resets the budget — that's how clients
  start a new turn.
- **Response headers** (`X-Pronaos-Agent-Turn-ID`, `…-Used-Tokens`,
  `…-Used-Cost-Hcents`, `…-Calls`, `…-Remaining-Tokens`,
  `…-Remaining-Cost-Hcents`) surface the running totals on every call
  — no admin-API round trip needed.
- **TTL self-eviction** (default 3600 s, per-team via
  `agent_turn_ttl_seconds`) — a client that never closes a turn won't
  leak Redis state indefinitely.
- **Fail-open.** Redis outage → tracker returns `allowed=True`. The
  monthly budget and rate limiter still apply, so degradation doesn't
  unlock unbounded spend.

### 5. Guardrails (ingress)

- **Regex detectors (always on when guardrails enabled):**
  `pii.email`, `pii.phone`, `pii.ssn`, `pii.credit_card`
  (Luhn-validated), `pii.ipv4`, `injection` (regex heuristic — not
  a classifier).
- **ML detector (opt-in):** `presidio` wraps Microsoft Presidio
  (spaCy NER + pluggable recognizers) to catch the long-tail PII
  regex misses — names, locations, dates, foreign formats. Each
  fired hit reports under its entity-specific rule name
  (`presidio.PERSON`, `presidio.LOCATION`, `presidio.DATE_TIME`,
  `presidio.EMAIL_ADDRESS`, ...). Lazy spaCy load on first scan;
  fail-open if the model or library is missing. Enable with
  `PRONAOS_PRESIDIO_ENABLED=true`.
- Actions: `BLOCK` | `REDACT` | `LOG_ONLY`. Default actions documented
  in `observability/README.md`.
- Per-team `guardrail_policy` JSON overrides defaults at request time
  (disable a rule, change its action, toggle Presidio with
  `"presidio": {"enabled": false}` shorthand). Managed via
  `pronaos-cli team set-guardrail-policy` or the admin API.

### 6. Cache lookup (two-tier, streaming-aware)

- **L1 exact (Redis)**: SHA-256 hash of `(messages, temperature,
  max_tokens)` under `cache:exact:{tenant_id}:{model}:{digest}`.
  Sub-millisecond hits.
- **L2 semantic (Qdrant)**: `sentence-transformers/all-MiniLM-L6-v2`
  embedding of the latest user message; cosine similarity ≥ threshold
  (default 0.95) under a `tenant_id` + `model` payload filter.
- **Read path:** L1 → L2 → provider, with promotion-on-L2-hit into L1.
- **Write path:** dual-write to L1 + L2 after a successful response.
- **Streaming-aware (Phase 28):** the SSE generator captures
  `(text, inter_chunk_delay_ms)` for every content chunk and persists
  them as `pronaos.stream_chunks` in the cached entry. On a `stream=true`
  cache hit, the gateway returns a `StreamingResponse` that replays
  the stored chunks as SSE at the original inter-chunk cadence (with
  the first chunk emitted immediately — the cache exists to eliminate
  the upstream's time-to-first-token, not reproduce it). Same cache
  entry serves both streaming and non-streaming reads.
- **Bypassed for:** `temperature > 0` and agent-loop turns (any
  message with `role:"tool"` or assistant `tool_calls`). Streaming
  alone is **no longer** a bypass condition. Bypass paths increment
  `pronaos_cache_lookups_total{result="skip"}`.
- Tenant isolation is enforced by construction (key path / payload
  filter), not by runtime check.
- Every backend fails open: cache outage degrades to a direct
  provider call.

### 7. Failover + circuit breaker

- Routing plan resolves the model prefix to a primary provider and
  zero or more fallbacks.
- Per-provider circuit breaker on the failover path:
  CLOSED → OPEN after 5 consecutive retryable failures → HALF_OPEN
  after 30 s → CLOSED on successful probe.
- **Two backends** (Phase 25): in-memory per-process (default, fastest)
  or Redis-backed shared (opt-in via
  `PRONAOS_CIRCUIT_BREAKER_DISTRIBUTED=true`). The Redis path uses
  atomic Lua scripts so concurrent failures from different replicas
  collapse cleanly — 5 replicas at threshold 5 trip on 5 *cumulative*
  failures, not 25.
- OPEN providers are skipped *before* any HTTP attempt — saves the
  connect-refused timeout (`pronaos_circuit_skipped_requests_total`).
- Auth errors deliberately don't trip the breaker — a misconfigured
  key isn't a provider-health signal.
- Trip events fire a `circuit.tripped` webhook to the tenant's
  configured receiver.
- **Request hedging (Phase 27)** — optional speculative parallel
  start. When the team's `hedge_delay_ms` is set, the executor waits
  that long for the primary; if it hasn't returned, an identical call
  is started against the next chain provider. `asyncio.wait
  FIRST_COMPLETED` selects the winner; the loser is cancelled (its
  httpx stream is closed). Trades a small upstream-call overhead for
  p99 latency reduction (Dean & Barroso, "The Tail at Scale"). Hedging
  respects the breaker — a hedge candidate whose breaker is OPEN is
  skipped. Headers `X-Pronaos-Hedged`, `X-Pronaos-Hedge-Winner`,
  `X-Pronaos-Hedge-Provider` surface the decision on hedged responses.

### 8. Provider call

- Streaming responses are passed through with backpressure; cancellation
  propagates cleanly to the upstream connection (httpx `async with
  stream(...)` close on `CancelledError`). Each cancellation ticks
  `pronaos_streams_cancelled_total`.
- Four native shapes:
  - **OpenAI-compat** (one adapter, 11 providers): direct REST + SSE.
  - **Anthropic native**: translates
    `tools`/`tool_choice`/`tool_use` ↔ `tool_calls` in both
    directions, streaming + non-streaming.
  - **AWS Bedrock** (Phase 42 + Phase 52): SigV4-signed over httpx
    (no boto3 client dep), per-model-family wire-shape translators
    (Anthropic / Llama / Nova / Mistral). Streaming uses AWS's
    `application/vnd.amazon.eventstream` binary protocol; Pronaos
    parses it with a pure-Python parser
    (`pronaos.providers.bedrock_eventstream`) that validates both
    prelude and message CRC32s, handles cross-chunk frame
    boundaries, and decodes 10 header value types. Per-family
    streaming-event translators emit canonical `ChatCompletionChunk`
    instances — see §8.1 below.
  - **GCP Vertex AI** (Phase 53): pure-Python GCP service-account
    JWT-bearer auth (no `google-auth` SDK on the hot path; reuses
    the `cryptography` library already pulled in by botocore for
    SigV4). Per-publisher wire-shape translators — Gemini's native
    `contents`/`parts`/`generationConfig` shape and Anthropic-on-
    Vertex's Messages shape with `anthropic_version="vertex-2023-10-16"`.
    Streaming via SSE for both families. See §8.2 below.
- Token usage captured from the provider's `usage` block; cost
  computed from the per-model pricing in `providers/catalog.py`.

#### 8.1 Bedrock streaming via AWS event-stream binary protocol (Phase 52)

- **Endpoint**: `POST /model/{id}/invoke-with-response-stream`
  (separate URL from non-streaming `/invoke`).
- **Accept header**: `application/vnd.amazon.eventstream` (negotiates
  the binary stream).
- **SigV4 signing**: same code path as non-streaming — `botocore.auth.SigV4Auth`
  signs the body bytes once; httpx streams the request.
- **Frame layout** (per AWS spec):
  ```
  [total_length: 4 BE u32][headers_length: 4 BE u32][prelude_crc32: 4 BE u32]
  [headers: variable, name-value pairs with 10 value types]
  [payload: variable]
  [message_crc32: 4 BE u32]   ← CRC32 of bytes [0, total_length-4)
  ```
- **Header types decoded**: 0 (true), 1 (false), 2 (int8), 3 (int16),
  4 (int32), 5 (int64), 6 (byte-array), 7 (string), 8 (timestamp),
  9 (UUID). For Bedrock, the headers we care about (`:message-type`,
  `:event-type`, `:content-type`) are all type-7 strings; the parser
  handles every type defensively.
- **Payload unwrap**: each frame's payload is
  `{"bytes": "<base64-of-utf8-json>"}` — Pronaos base64-decodes and
  re-parses as the per-family event.
- **Per-family streaming-event translators**:
  | Family | Event stream | Visible chunks | Terminal carries |
  |---|---|---|---|
  | `anthropic.*` | `message_start` / `content_block_*` / `message_delta` / `message_stop` | `content_block_delta.text_delta` text; tool_use args accumulated across `input_json_delta` | `finish_reason`, `prompt_tokens`, `completion_tokens`, assembled `tool_calls[]` |
  | `meta.*` (Llama) | Per-frame `{generation, prompt_token_count, generation_token_count, stop_reason}` | Each frame with non-empty `generation` | Final frame's `stop_reason` + counts |
  | `amazon.*` (Nova) | `messageStart` / `contentBlockDelta` / `contentBlockStop` / `messageStop` / `metadata` | `contentBlockDelta.delta.text` | `messageStop` carries `finish_reason`; `metadata` (if any) on a follow-up chunk |
  | `mistral.*` | Per-frame `{outputs: [{text, stop_reason}]}` | Each frame with non-empty text | Final frame's `stop_reason` |
- **Exception frames**: `:message-type=exception` frames mid-stream
  raise `ProviderError(status=502, retryable=True)` so the failover
  layer treats them as 502s and can fail over to a different
  provider.
- **CRC32 mismatches** raise `ProviderError(status=502,
  retryable=False)` — silent stream corruption never reaches the
  consumer.
- **Mid-stream connection cuts** drop trailing partial bytes
  silently; already-yielded chunks remain valid.

#### 8.2 GCP Vertex AI native adapter (Phase 53)

- **Auth**: GCP service-account JWT-bearer flow. The operator-provided
  SA JSON file contains an RSA-2048 private key + the SA's email +
  the project ID. The auth helper:
  1. Signs a short-lived RS256 JWT (`iss=client_email`,
     `scope=cloud-platform`, `aud=oauth2.googleapis.com/token`,
     `iat=now`, `exp=now+3600`) using `cryptography.hazmat.primitives`.
  2. POSTs `grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer`
     + `assertion=<jwt>` to the token endpoint as
     `application/x-www-form-urlencoded`.
  3. Receives `{access_token, expires_in}`. Caches the token with
     a 5-minute leeway window — refreshes proactively before
     expiry under an `asyncio.Lock` (only one refresh runs even if
     N concurrent requests race the expiry check).
- **No `google-auth` SDK**. Same posture as the Bedrock adapter:
  pure-Python on the hot path. `cryptography` is already a
  transitive dep through botocore (needed for SigV4); reusing it
  for RS256 JWT signing adds zero new package bytes.
- **URL routing**: per-region, per-project, per-publisher —

      https://{region}-aiplatform.googleapis.com/v1
        /projects/{project}/locations/{region}
        /publishers/{publisher}/models/{model}:{action}

  Publishers in this phase: `google` (Gemini family) and `anthropic`
  (Claude on Vertex). Actions: `generateContent` (non-streaming),
  `streamGenerateContent?alt=sse` (streaming Gemini), `streamRawPredict`
  (streaming Anthropic-on-Vertex).
- **Per-publisher wire shapes**:

  | Family | Body shape | Notes |
  |---|---|---|
  | Gemini (`google`) | `{contents: [...], systemInstruction: {parts: [...]}, generationConfig: {maxOutputTokens, temperature}, tools: [{functionDeclarations: [...]}]}` | NOT OpenAI `messages`; role `model` for assistant; system hoisted out of contents |
  | Claude on Vertex (`anthropic`) | Anthropic Messages shape with `anthropic_version="vertex-2023-10-16"` and **no** `model` field (model lives in the URL) | Different version string from direct Anthropic (`2023-06-01`) and Bedrock (`bedrock-2023-05-31`) |

- **Streaming SSE**: both publishers emit `data: <json>\n\n`
  events. Pronaos parses them with `aiter_lines()` + per-family
  event translators that thread state across chunks. Gemini events
  are flat (one `candidates[0].content.parts[].text` per chunk,
  terminal carries `finishReason` + `usageMetadata`). Anthropic-on-
  Vertex events are the canonical Anthropic streaming sequence
  (`message_start` → `content_block_*` → `message_delta` →
  `message_stop`), with tool-use args accumulated across
  `input_json_delta` frames identically to the direct adapter.
- **Cost math** uses catalog pricing for the resolved `publisher/model`
  (Gemini 1.5 Flash: $0.075/Mtok input; Gemini 1.5 Pro: $1.25/Mtok
  input; Claude-on-Vertex at direct Anthropic rates).
- **Failure modes**:
  - **401/403** → typed `AuthError` with the GCP `error.message`
    surfaced. Most common cause: SA missing `roles/aiplatform.user`
    or `aiplatform.googleapis.com` not enabled on the project.
  - **429** → `RateLimitError` (Vertex quotas).
  - **5xx** → `ProviderError(status=502, retryable=True)` — the
    failover layer can retry on a different provider.
  - **Bad SA JSON** at startup → `ProviderNotConfiguredError`;
    never reaches the request path.
  - **OAuth2 token-exchange failure** (typical: clock skew →
    `invalid_grant`) → typed `VertexAuthError` rewrapped as `AuthError`
    at the adapter boundary.

### 9. Guardrails (egress)

- Scans the assistant response for PII leak-back. Can only REDACT —
  by the time we're here the upstream call has happened.
- Runs on both streaming and non-streaming paths (the streaming
  variant scans the assembled content at stream close).
- Toxicity / banned-output detectors are not shipped.

### 10. Audit, usage, observability

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
  cache, guardrail (now including per-Presidio-entity labels),
  circuit, preflight, stream-cancellation, routing-decisions
  (Phase 21), agent-turn denials (Phase 30), embeddings
  (Phase 31), rerank (Phase 32), singleflight followers
  (Phase 33), prompt-cache token attribution (Phase 34) — 25
  metric families total. Two pre-provisioned
  Grafana dashboards (Overview = 11 panels, FinOps = 8 panels).
- **OTEL spans**: auto-instrumented FastAPI / httpx / SQLAlchemy +
  named `pronaos.quota.check` and `pronaos.provider.call` spans
  with cost/token attributes for trace-time FinOps queries.

### 11. Embeddings endpoint (Phase 31)

The same pipeline applies to `POST /v1/embeddings` — auth, allowlist,
preflight, ingress guardrails (on the input text), cache, audit, usage
record — with two differences relative to chat:

- **No streaming, no failover.** Embeddings are a single
  request/response. There's no SSE replay path; the cache stores
  vectors verbatim under
  `cache:exact:{tenant_id}:{model}:{sha256(input, dimensions)}`.
- **Five backend shapes** under one abstract `EmbeddingProvider`:
  OpenAI-compatible (covers OpenAI / Mistral / OpenRouter), Cohere
  (`/v2/embed` with `texts` field + `input_type`), Voyage (`input`
  with optional `input_type` query/document hint), local
  sentence-transformers (in-process, no API key — same model the
  semantic cache loads). The hint comes from `embedding_shape` on
  each catalog entry; the registry picks the matching adapter.
- **Cost accounting** uses per-million-input-token pricing (no
  completion tokens). Spend lands in the same
  `provider_cost_hcents_total` counter and `usage_records.cost_hcents`
  column as chat, so `pronaos-cli team chargeback` aggregates both
  endpoints in one report.
- **Metrics**: `pronaos_embedding_requests_total{provider,model,status}`,
  `pronaos_embedding_request_duration_seconds`, `pronaos_embedding_tokens_total`,
  `pronaos_embedding_cache_hits_total{model}`.

### 12. Rerank endpoint (Phase 32)

`POST /v1/rerank` completes the RAG triad (embed → retrieve → rerank).
Same pipeline reuse as embeddings: auth, allowlist, preflight, ingress
guardrails (on the query + every document), L1 exact cache, audit,
usage record.

- **Two backend shapes.** Cohere `/v2/rerank` (`top_n` + per-call
  billing in "search units"); Voyage `/v1/rerank` (`top_k` + per-token
  billing). Public request shape uses Cohere's `top_n`; the Voyage
  adapter translates to `top_k` internally.
- **Cache key**:
  `cache:exact:{tenant_id}:{model}:{sha256({"type":"rerank","query":...,"documents":[...],"top_n":N,"return_documents":bool})}`.
  Identical search re-issued = byte-identical scores at zero upstream
  cost.
- **Two pricing models, one cache.** Cohere bills per call regardless
  of document count; Voyage bills per token. Both go to zero on cache
  hit.
- **Metrics**: `pronaos_rerank_requests_total{provider,model,status}`,
  `pronaos_rerank_request_duration_seconds`, `pronaos_rerank_cache_hits_total{model}`,
  plus the chat-side `pronaos_provider_cost_hcents_total` for unified
  FinOps reporting across chat + embedding + rerank.

### 13. Singleflight dedup (Phases 33 + 36)

Concurrent identical requests on a cold cache (RAG ingestion bursts,
retry storms, parallel agent tool calls) currently fire N independent
upstream calls — 99 of 100 are wasted. The `SingleflightRegistry` at
`app.state.singleflight` collapses them:

- The first arrival becomes the **leader**: runs the upstream call,
  writes the cache, sets a shared `asyncio.Future`.
- Subsequent arrivals in the same race window become **followers**:
  await the leader's future and share its result.
- On leader failure, the exception propagates to all followers
  (standard Go singleflight semantics); the next arrival after
  failure takes a fresh leader slot.

Wrapped around the cache-miss path on `/v1/embeddings` and `/v1/rerank`.
Chat is a documented follow-up (streaming + hedging + A/B paths
warrant their own phase). Per-call audit + usage record stays
per-request; followers charge `cost_hcents=0` so `usage_records`
reflects actual upstream spend faithfully.

- **Tenant-isolated keys**: `sha256({tenant_id, model, cache_payload})`
  — two tenants embedding the same text do NOT share a leader.
- **Metric**: `pronaos_singleflight_followers_total{endpoint}` counts
  deduplicated requests. Empirical claim #20 shows 49/50 followers
  on a 50-burst.

**Phase 36 — cross-replica singleflight.** Two backends behind one
interface (mirrors the Phase 25 distributed circuit breaker pattern):

- In-memory (default): asyncio futures + dict in one process.
  Lower latency, no infra dependency.
- Redis-backed (opt-in via `PRONAOS_SINGLEFLIGHT_DISTRIBUTED=true`):
  atomic `SET NX` on `pronaos:singleflight:{key}` with JSON envelope
  carrying state ∈ {pending, done, failed}; followers poll every
  ~50 ms; TTL bounds the absolute wait so a dead leader doesn't
  deadlock followers. Same-replica fast path (local lock + futures
  dict) catches concurrent same-process callers BEFORE hitting Redis.

Leader exception serialised + propagated to followers as
`CrossReplicaLeaderError`. Empirical claim #23 demonstrates 50
concurrent calls across 5 simulated replicas collapsing to one
upstream call.

### 14. Prompt-cache FinOps (Phases 34, 35, 55)

Prompt caching is surfaced uniformly across **four** deployment paths:
direct Anthropic + direct OpenAI + Anthropic-on-Bedrock +
Anthropic-on-Vertex. Same response headers, same metric labels, same
weighted pricing math — the chat handler is provider-agnostic.

**Direct Anthropic (Phase 34)** — `cache_control` blocks, ~90% discount:

- Adapter extracts `cache_creation_input_tokens` (1.25x billed) +
  `cache_read_input_tokens` (0.10x billed) from streaming
  (`message_start` event) and non-streaming response root.
- `AnthropicProvider.cost_cents` applies the weighted pricing.
- Anthropic's wire shape already EXCLUDES cached tokens from
  `input_tokens` — no adapter normalisation needed.

**Direct OpenAI (Phase 35)** — auto-cached prefixes ≥1024 tokens, 50%
discount, no opt-in:

- Adapter extracts `usage.prompt_tokens_details.cached_tokens` from
  streaming + non-streaming responses.
- OpenAI's wire shape INCLUDES cached tokens in `prompt_tokens`;
  adapter SUBTRACTS so all downstream consumers see the non-cached
  portion. This keeps the chat handler provider-agnostic.
- `OpenAICompatibleProvider.cost_cents` applies the 0.5x discount;
  no cache-write premium (OpenAI doesn't bill it).
- Other OpenAI-compat upstreams (Groq, DeepSeek, etc.) don't expose
  the field — extraction falls through to 0, no behavioural change.

**Anthropic-on-Bedrock + Anthropic-on-Vertex (Phase 55)** — same
`cache_control` semantics as direct Anthropic, same weighted pricing,
mirrored across both cloud adapters:

- Bedrock parser `_parse_anthropic_response` + streaming translator
  `_translate_anthropic_stream_event` read the same
  `cache_creation_input_tokens` + `cache_read_input_tokens` fields
  from the usage block. `BedrockProvider.cost_cents` applies the
  1.25x/0.10x weighted math behind a `family == "anthropic"` gate.
- Vertex parser `_parse_anthropic_on_vertex_response` + streaming
  translator `_translate_anthropic_on_vertex_stream_event` mirror
  Bedrock's behaviour. `VertexProvider.cost_cents` gates on
  `publisher == "anthropic"`.
- Non-Anthropic publishers (Llama/Nova/Mistral on Bedrock, Gemini on
  Vertex) fall through to plain math — cache args ignored entirely.
  The gate keeps the wrong-multiplier bug from bleeding across
  families.
- Closes a real under-reporting gap: pre-Phase-55, naive accounting
  made cache writes look free AND charged cache reads full price.
  Empirical Claim #42 quantifies: 144 hcents (Phase-55 truth) vs
  412 hcents (naive full-price) on a representative Haiku 3.5
  workload — a 65% under-reporting bug closed.

**Uniform surfacing across all four paths:**

- **Response headers stamped** when cache stats are non-zero:
  `X-Pronaos-Prompt-Cache-{Read,Write}-Tokens` +
  `X-Pronaos-Prompt-Cache-Saved-Hcents`.
- **Response body's `pronaos` block** carries cache fields alongside
  `cost_hcents`.
- **`usage_records.cost_hcents`** is the post-discount cost — the
  team is billed at the cached rate, no double-charging.
- **Metric**: `pronaos_prompt_cache_tokens_total{provider, model, type}`
  where `type ∈ {read, write}`. Same counter, all four deployment
  paths populate it.

### 14.5. Reasoning-token FinOps (Phase 56)

Reasoning models (Anthropic extended thinking, OpenAI o1/o3,
DeepSeek R1, Gemini 2.0/2.5 thinking) surface tokens-the-user-
never-saw differently. Pronaos extracts them uniformly via two new
`ChatCompletionChunk` fields: `reasoning_tokens` and `reasoning_content`.

**Anthropic direct + Bedrock + Vertex** — `type: "thinking"` content
blocks:

- Parser extracts thinking text into `reasoning_content`. Streaming
  uses a per-block-index accumulator over `content_block_start`
  (type=thinking) + `content_block_delta` (delta.type=thinking_delta).
- Anthropic does NOT expose a separate thinking-token count in
  `usage` — thinking IS counted in `output_tokens`. Pronaos
  estimates `reasoning_tokens` via ceil(len/4) — purely for
  visibility, NOT for billing (which already happens through
  `output_tokens`).
- Thinking text is body-only: it lands on `chunk.reasoning_content`
  but NEVER on `chunk.content_delta`. SSE-decoding clients see
  user-visible text only on the content stream.

**OpenAI o-series + DeepSeek R1** — `usage.completion_tokens_details.reasoning_tokens`:

- Adapter reads the field on both streaming + non-streaming.
- Already INCLUDED in `completion_tokens` → cost math unchanged.
- DeepSeek additionally ships `message.reasoning_content` (CoT
  text) which Pronaos preserves; OpenAI o-series doesn't (intentional
  upstream choice).

**Vertex Gemini thinking** — `usageMetadata.thoughtsTokenCount` —
**the correctness fix**:

- Gemini EXCLUDES `thoughtsTokenCount` from `candidatesTokenCount`.
  Pre-Phase-56, Pronaos was billing on `candidatesTokenCount`
  alone → up to 96% under-charge per thinking-mode request.
- Phase 56 ADDS `thoughtsTokenCount` to `completion_tokens` so
  downstream cost math (which multiplies `completion_tokens` ×
  output_rate) bills correctly. The raw count also lands in
  `reasoning_tokens` for visibility.

**Uniform surfacing across all five paths:**

- **Response header**: `X-Pronaos-Reasoning-Tokens` (count only — CoT
  text is body-only because header intermediaries can log header
  values).
- **Response body's `pronaos` block**: `reasoning_tokens` +
  `reasoning_content` fields (latter only when the upstream shipped
  CoT text).
- **Metric**: `pronaos_reasoning_tokens_total{provider, model, source}`
  with `source = upstream | estimated`. Splits provider-reported
  exact counts (OpenAI / DeepSeek / Gemini) from Pronaos-inferred
  char-length estimates (Anthropic direct / Bedrock / Vertex
  Anthropic).
- **No behavioural change** for non-reasoning models: the fields
  stay at 0/None, the header is not stamped, the metric is not
  incremented.

### 14.6. Reasoning-aware routing (Phase 57)

Composes Phase 56's per-call reasoning signal into the router as a
new `reasoning-aware-cheapest` strategy. Mirrors Phase 47's
prompt-cache-aware shape end-to-end:

- **`ReasoningObserver`** (`src/pronaos/core/reasoning_observer.py`):
  Redis-backed rolling totals per `(team_id, fqmn)`. One hash per
  team with three fields per fqmn: `completion`, `reasoning`, `n`.
  14-day TTL refreshed on every write. Fail-open on Redis outage.
  Recorded on every successful chat call (including
  `reasoning_tokens == 0` — that's signal too).
- **`ReasoningAwareCostScorer`**: multiplies each candidate's nominal
  output rate by `1 + observed_reasoning_ratio`. Input rate
  unchanged. Returns nominal output when below `min_samples` or
  when observation is missing.
- **`filter_by_reasoning_ratio`**: excludes candidates whose observed
  ratio exceeds the team's `max_ratio` cap (when set). Candidates
  below `min_samples` are NEVER excluded — their ratio is
  unreliable, so the cap doesn't bite.
- **Per-team thresholds**: `teams.reasoning_aware_min_samples`
  (default NULL → 20), `teams.reasoning_aware_max_ratio` (default
  NULL → no cap).
- **Admin endpoints**: GET `/v1/admin/team/{id}/reasoning-stats`
  (snapshot + thresholds), PUT `.../reasoning-config` (set
  thresholds), DELETE `.../reasoning-stats` (wipe observations).
- **Same opt-in semantics** as Phases 11/33/47: teams that don't
  set the strategy see zero behavioural change.

The routing-strategy matrix is now complete: cost / quality /
tool-use / prompt-cache / reasoning — five aware-strategies, all
plugged into the same `select_model` pipeline with the same
fail-open + opt-in posture.

### 15. Outbound webhook events

- Tenant configures one webhook URL + shared secret. Events fire as
  HTTP POSTs with `X-Pronaos-Signature: sha256=<hex>` (HMAC-SHA256,
  GitHub-webhook-compatible scheme).
- Three event types: `quota.exhausted`, `circuit.tripped`,
  `audit.chain_broken`.
- Retry up to 3 attempts on 5xx + connection errors with exponential
  backoff (0.5 s → 1 s → 2 s). 4xx → no retry.
- Fire-and-forget asyncio task with a strong-reference set so the
  task isn't garbage-collected mid-flight.

### 16. Native MCP server (Phase 48 SSE + Phase 50 stdio)

#### 16.1 SSE transport (Phase 48)

- `PRONAOS_MCP_ENABLED=true` mounts an MCP server at
  `/v1/mcp/sse` (SSE handshake) + `/v1/mcp/messages` (client back-
  channel). Remote / containerised MCP-speaking clients target the
  gateway directly.
- Tools advertised: `pronaos.chat`, `pronaos.embed`, `pronaos.rerank` —
  JSON Schemas mirror the REST body shapes.
- Bearer-token auth at the SSE handshake validates a standard
  Pronaos API key (same code path as REST), requires `chat:write`,
  and stashes the token into a per-asyncio-task ContextVar.
- Each `tools/call` dispatcher reads the ContextVar and forwards via
  **loopback HTTP** to `/v1/chat/completions` (or `/v1/embeddings` /
  `/v1/rerank`). That preserves the full middleware chain — auth,
  quotas, guardrails, cache, routing, audit, prompt-cache surfacing —
  so MCP traffic inherits every gateway feature uniformly with the
  REST path. The trade-off: one extra TCP round-trip per MCP tool
  call (loopback, sub-millisecond).
- Disabled by default; the routes return 503 when the flag is off
  so the URL surface stays stable across operator flips.

#### 16.2 Stdio transport (Phase 50)

- IDE-class MCP clients — Claude Code, Anthropic Desktop, Cursor,
  Windsurf, Continue — spawn the MCP server as a local subprocess
  and exchange MCP JSON-RPC frames over stdin/stdout. To register
  with those clients, the gateway needs a binary they can spawn.
- `pronaos-mcp-proxy` is that binary. Registered as a console-script
  entry in `pyproject.toml [project.scripts] = "pronaos.mcp:_stdio_main"`,
  so `pip install pronaos` lands it on `$PATH` (venv `bin/` on POSIX,
  `Scripts/` on Windows). The IDE client constructs a
  `StdioServerParameters(command="pronaos-mcp-proxy", args=[...])`
  exactly as it would for any other stdio MCP server.
- The proxy is a separate process from the gateway. Architecture:
  ```
  Claude Code (or any IDE-MCP client)
    │  subprocess.Popen("pronaos-mcp-proxy", args=[...], stdin=PIPE, stdout=PIPE)
    ▼
  pronaos-mcp-proxy
    │  mcp.server.stdio.stdio_server() → JSON-RPC over stdin/stdout
    │  PronaosMcpServer (same adapter as SSE transport)
    │  bearer token from --api-key / --api-key-file → ContextVar
    ▼
  Pronaos gateway (HTTP service, may already be running)
    │  loopback POST /v1/chat/completions
    │  full middleware chain: auth, quotas, guardrails, cache, routing, audit
    ▼
  upstream provider (Anthropic / OpenAI / Groq / ...)
  ```
- CLI surface:
  - `--gateway-url <url>` — base URL of the Pronaos HTTP gateway.
    Default `http://127.0.0.1:8080`.
  - `--api-key <token>` — inline bearer (NOT recommended on shared
    machines; visible in `ps`).
  - `--api-key-file <path>` — read first line as the bearer. Mutually
    exclusive with `--api-key`. Recommended path.
- Bearer-token lifecycle: resolved once at startup, set into the
  same per-task ContextVar the SSE transport uses, reset via a
  `try / finally` block on clean shutdown. One bearer per spawned
  proxy — different teams / different keys → different `claude mcp
  add` registrations (e.g. `pronaos-prod`, `pronaos-dev`).
- Failure modes:
  - Missing token (neither flag) → `SystemExit(2)` with actionable
    message BEFORE any MCP frame is read.
  - Empty / unreadable `--api-key-file` → `SystemExit(2)` with
    explicit "is empty" or "cannot read" message.
  - `KeyboardInterrupt` (client closed the subprocess) →
    `sys.exit(0)` (clean).
- Gateway lifecycle decoupling: the proxy is a child of the IDE
  client, not the gateway. Gateway restarts don't tear down MCP
  client connections; the proxy reconnects on the next tool call.
- Registration with Claude Code (one line):
  ```bash
  claude mcp add pronaos -- pronaos-mcp-proxy \
      --gateway-url http://127.0.0.1:8080 \
      --api-key-file ~/.config/pronaos/api-key
  ```
- Same shape works for Anthropic Desktop, Cursor, Windsurf,
  Continue, and any future stdio-mode MCP client — the
  `StdioServerParameters` contract is the standard.

#### 16.3 Streaming progress notifications (Phase 51)

- Closes the streaming honest-limit in §16.1 + §16.2: a chat call
  through MCP no longer waits for the full upstream response
  before producing the `CallToolResult`. The MCP spec defines
  `notifications/progress` for incremental progress; the client
  passes `_meta.progressToken` on its `tools/call`, and the server
  is permitted (not obligated) to emit progress notifications
  carrying that token.
- Pronaos's chat tool uses the token's presence as the signal to
  take a streaming forwarding branch:
  ```
  _read_progress_token() returns token? → _forward_chat_streaming(...)
                                       │
                                       ▼
                                forces body.stream = True
                                       │
                                       ▼
                                POST /v1/chat/completions stream=true
                                       │
                                       ▼
                                async for SSE chunk:
                                  parse delta + finish_reason + usage
                                  session.send_progress_notification(
                                      progress_token=token,
                                      progress=N (monotonic),
                                      message=delta_content,
                                      related_request_id=ctx.request_id,
                                  )
                                  record_mcp_streaming_chunk(transport)
                                       │
                                       ▼
                                synthesize final ChatCompletion shape
                                from accumulated deltas + finish_reason + usage
                                ↳ stamped with pronaos.mcp_streamed=true
                                ↳ returned as a single TextContent
  ```
- The same branch serves both SSE and stdio transports. The
  ``PronaosMcpServer(transport="sse"|"stdio")`` ctor labels
  Prometheus metrics so dashboards can split streaming traffic by
  transport.
- New counters:
  - ``pronaos_mcp_streaming_chunks_total{transport}`` — +1 per
    chunk forwarded as a progress notification.
  - ``pronaos_mcp_streaming_sessions_total{transport, result}`` —
    +1 per call that took the streaming branch.
    ``result`` ∈ {``ok``, ``upstream_error``, ``mid_stream_error``}.
- Non-streaming behaviour is unchanged: a client that doesn't
  supply ``_meta.progressToken`` falls through to the existing
  ``_forward`` path, no SSE parsing happens, no notifications fire,
  and the final ``CallToolResult`` does NOT carry the
  ``pronaos.mcp_streamed`` marker. The streaming feature is
  surgically opt-in.
- Failure modes:
  - **Upstream non-200 before any chunk** → records
    ``sessions_total{result="upstream_error"}``, returns the
    upstream's error body verbatim as the final
    ``CallToolResult``; zero progress notifications fire.
  - **Mid-stream exception** (network drop, malformed chunk past
    the JSON-decode guard) → records
    ``sessions_total{result="mid_stream_error"}``, returns a
    structured error payload with ``partial_content`` +
    ``progress_index`` so the client can decide whether to use the
    notifications it already received. Progress notifications
    already delivered remain valid.
- **stdio transport caveat for metric visibility**: the proxy is a
  separate process, so its Prometheus registry is not what
  ``/metrics`` on the gateway exposes. The streaming counters tick
  inside the proxy subprocess and are not externally observable on
  stdio runs. For SSE-transport MCP (§16.1), the MCP server lives
  in the gateway process and the same counters tick visibly. The
  live verify script documents this honestly and the captured
  progress notifications are the empirical proof, not the metric
  delta.

#### 16.4 MCP client federation (Phase 54)

- Closes the bidirectional MCP narrative. §16.1–16.3 made Pronaos
  an MCP **server**; this section is Pronaos as MCP **client**.
- **Trigger**: a chat request carries
  ``body.pronaos_mcp_servers = [{name, command, args, env}]``.
- **Gate**: per-team ``mcp_client_enabled`` flag (Migration 0023).
  Off by default — stdio MCP servers spawn subprocesses on the
  gateway host, so subprocess execution is security-sensitive.
- **Loop architecture** (in ``api/v1/chat.py::_run_mcp_federation_loop``):
  ```
  async with open_federation(specs) as federation:
      augmented_tools = body.tools + federation.federated_tool_schemas()
      inner_body = {**body without pronaos_mcp_servers, "tools": augmented_tools}
      for iteration in 1..max_iterations (default 5, cap 10):
          loopback POST /v1/chat/completions with inner_body
          if any tool_call has prefix matching a federated server:
              dispatch to federation, append synthetic `tool` role message
              continue
          else: return response
      return last response with X-Pronaos-MCP-Max-Iterations-Reached
  ```
- **Federation primitives** (in ``mcp/client_federation.py``):
  - ``McpServerSpec`` — validated dataclass (name + command + args + env);
    name must be ``[A-Za-z0-9_-]+`` to avoid clashing with the
    ``{server}.{tool}`` prefix scheme.
  - ``McpFederation`` — async context manager that opens
    connections SEQUENTIALLY (anyio task groups in
    ``stdio_client`` reject cross-task close, so parallel
    ``asyncio.gather`` is unsafe here).
  - ``open_federation(raw_specs)`` — convenience that parses +
    opens in one step.
  - Per-server failure isolation: an unopenable server is recorded
    in ``failed_server_names`` but doesn't fail the whole
    federation; ``call_tool`` on it returns an error result the
    LLM can recover from.
- **Tool namespace**: every discovered tool's name is rewritten to
  ``{spec.name}.{tool.name}``. Routing peels the prefix to look up
  the right session. Avoids collisions between servers that happen
  to expose tools with the same name.
- **Loopback HTTP, not direct re-entry**: same pattern Phase 48's
  MCP server uses. The inner POST goes through the full middleware
  chain — auth, quotas, guardrails, cache, routing, audit — on
  every iteration. Inner calls have ``pronaos_mcp_servers`` stripped
  from the body so they take the regular non-federated path.
- **Iteration cap**: default 5; client can override via
  ``X-Pronaos-MCP-Max-Iterations`` header (clamped to 10).
  Prevents runaway tool loops.
- **Response telemetry headers**:
  - ``X-Pronaos-MCP-Federated-Servers`` — comma-joined names of
    servers that opened cleanly
  - ``X-Pronaos-MCP-Failed-Servers`` — comma-joined names that
    failed to open (typically: binary not found)
  - ``X-Pronaos-MCP-Iterations`` — how many upstream calls the
    federation loop fired
  - ``X-Pronaos-MCP-Max-Iterations-Reached`` — stamped when the
    cap was hit (the LLM is in an unstoppable tool loop)
- **Metrics**:
  - ``pronaos_mcp_federated_tool_calls_total{server, tool, result}``
    — per-tool dispatch counter; result ∈ {ok, upstream_error,
    federation_error}
  - ``pronaos_mcp_federation_sessions_total{result}`` — per-session
    outcome counter; result ∈ {ok, max_iterations, invalid_spec}
- **Failure modes**:
  - ``mcp_client_disabled`` (422): team flag is off but request
    carries ``pronaos_mcp_servers``
  - ``mcp_invalid_spec`` (422): a server spec failed validation
    (duplicate name, bad name format, missing command, etc.)
  - Per-server failure → tool returns error content; LLM agent
    loop sees the error and can react
  - Max iterations → the loop returns the last response with a
    header marker; not an error (the LLM is still producing
    valid completions, just in a tool-call loop)

#### 16.5 Streaming MCP federation (Phase 58)

- Closes the Phase 54 documented honest-limit:
  ``stream=true`` + ``pronaos_mcp_servers`` together used to return
  HTTP 422 ``mcp_streaming_unsupported``. IDE-class clients (Claude
  Code, Cursor, Continue) that always stream couldn't use
  federation. Phase 58 removes the gate.
- **Design**: reuse §16.4's well-tested non-streaming
  ``_run_mcp_federation_loop`` end-to-end, then synthesize an
  OpenAI-shape SSE stream from the final payload. The wrapper lives
  in ``api/v1/chat.py::_run_mcp_streaming_federation``.
- **SSE wire shape** the wrapper emits:
  1. First chunk: ``{delta: {role: "assistant"}, finish_reason: null}``
  2. Content chunks (64 chars each, matching Phase 28's
     streaming-replay chunking): ``{delta: {content: "..."}, finish_reason: null}``
  3. Terminal chunk: ``{delta: {tool_calls?: [...]}, finish_reason: "stop"}``
  4. ``data: [DONE]`` sentinel
- **Header propagation**: federation telemetry headers from the
  inner loop's Response (``X-Pronaos-MCP-Federated-Servers``,
  ``-Iterations``, ``-Failed-Servers``,
  ``-Max-Iterations-Reached``) all flow through onto the
  StreamingResponse. A new ``X-Pronaos-MCP-Streamed: 1`` marker is
  stamped so dashboards / log scrapers can distinguish streaming
  federation from regular chat streaming.
- **Metric**:
  ``pronaos_mcp_streaming_federation_sessions_total{result}`` —
  ticks alongside the existing
  ``mcp_federation_sessions_total{result}`` (kept intact to preserve
  dashboard time-series). Same ``result`` taxonomy plus ``error``
  for unexpected HTTPException sources. Sum of both counters across
  the same result label = (non-streaming sessions + 2 × streaming
  sessions), which the metric docstring spells out — operators read
  each counter in isolation.
- **Honest-limit (documented in CLAIMS #45)**: v1 buffers the
  federation loop's final response, then synthesizes SSE from it.
  TTFT equals full federation loop latency, not first-token from
  the upstream. True mid-stream tool_call routing (accumulating
  tool_call fragments from a stream + dispatching mid-stream) is a
  future phase that requires a larger refactor of the streaming
  adapter.

### 16.6 Async batches API at 50% pricing (Phase 59)

- New `POST /v1/batches` / `GET /v1/batches/{id}` / `GET /v1/batches/{id}/results` / `POST /v1/batches/{id}/cancel` endpoints, all gated on per-team `batches_enabled` (default OFF — operator opt-in because batch quota usage is non-trivial).
- Provider chosen from the FIRST request's model with same-batch consistency:
  - `openai/gpt-4o-mini`, `gpt-4o`, `o1`, `o3` → OpenAI Batches API
  - `anthropic/claude-*`, bare `claude-*` → Anthropic Messages Batches API
  - Mixed-provider batches → HTTP 422 `batch_mixed_providers`.
- The OpenAI client uploads the JSONL via `POST /v1/files` (purpose=batch) then creates the batch via `POST /v1/batches`. The Anthropic client translates the OpenAI-shape `{custom_id, body}` to Anthropic-shape `{custom_id, params}` and submits via `POST /v1/messages/batches`. Both clients implement the same `BatchClient` protocol: `submit` / `poll` / `retrieve_results` / `cancel` / `aclose`.
- One row per batch in the new `batches` table tracks the lifecycle:
  - Pronaos-normalised status: `validating → in_progress → finalizing → completed | failed | expired | cancelled` (both providers' vocabularies fold onto this set)
  - counts: `request_count` / `completed_count` / `failed_count`
  - tokens + cost (computed at the half-rate)
  - `input_payload`: original inline JSONL (for replay + audit)
  - `output_payload`: result JSONL pulled from the provider on completion
- Single per-process `BatchWorker` asyncio task launched from the FastAPI lifespan. It wakes every `BATCHES_POLL_INTERVAL_SECONDS` (default 60), `SELECT`s non-terminal rows, polls each via the provider client, syncs status + counts back to the row, and on terminal-completed transitions fetches the result JSONL, parses it, and writes one `UsageRecord` per successful sub-request with:
  - `status="batch_success"` (so chargeback queries split sync vs batch spend by `WHERE status LIKE 'batch_%'`)
  - `request_id="{batch_id}#{custom_id}"` (per-request drilldown without a new column)
  - `cost_hcents` computed by `batch_cost_hcents` at half the catalog rate
- Operators running multiple gateway replicas should disable the worker on N-1 replicas (`BATCHES_WORKER_ENABLED=false`); per-request usage rows are keyed by `{batch_id}#{custom_id}` so duplicate-run noise surfaces as `IntegrityError-then-skip` (no double-billing), but the recommended posture is one worker.
- Half-rate cost math (integer-clean, no float drift):
  - `BATCH_COST_MULTIPLIER_NUMERATOR = 50`, `BATCH_COST_MULTIPLIER_DENOMINATOR = 100`
  - `tokens × hcents_per_mtok × 50 // (1_000_000 × 100)` over the catalog's per-Mtok rates
  - Verified mechanically: `gpt-4o-mini` at (pt=1_000_000, ct=500_000) sync=45000 hcents, batch=22500 hcents, exactly half
- Per-team CLI:
  - `pronaos-cli team set-batches <id> --enable / --disable / --show` flips the flag
  - `pronaos-cli batch list [--team-id ...] [--status ...] [--limit ...]` shows recent rows
  - `pronaos-cli batch show <batch_id>` prints one row in detail
- Metric `pronaos_batch_jobs_total{provider, status}` ticks on submit (`status=validating`) + on each terminal transition. Submitted − terminal = in-flight.
- Honest limits documented in `CLAIMS.md` #46:
  - Mocked-live verify, not real-live (24-hour wait is impractical for CI)
  - Single-worker polling posture (no leader election)
  - ~~v1 ships chat-only; embedding batches is a future phase~~ — **closed by Phase 60 (Claim #47)**
  - No real-time progress streaming; operators poll `GET /v1/batches/{id}`

### 16.7 Async embedding batches at 50% pricing (Phase 60)

- Phase 60 extends the same `POST /v1/batches` surface to accept `endpoint: "/v1/embeddings"`. RAG corpus ingestion (the canonical embedding workload) now runs at 0.5× the per-token rate end-to-end.
- `batch_cost_hcents` takes an `endpoint` kwarg. When `endpoint == "/v1/embeddings"`, the lookup routes to `entry.embedding_pricing` (a separate dict from `entry.pricing` — without the discriminator the lookup silently returns 0, masking the bug entirely).
- `OpenAIBatchClient.submit` accepts the same `endpoint` kwarg and plumbs it into the upstream `POST /v1/batches` body. `AnthropicBatchClient.submit` raises `ValueError` if asked for anything other than chat (Anthropic ships no embeddings API).
- `provider_from_model` learns the `text-embedding-*` prefix pattern alongside the existing `gpt-/o1/o3` and `claude*` fallbacks. Bare names like `text-embedding-3-small` route to OpenAI; explicit `openai/text-embedding-*` continues to work.
- API layer:
  - Endpoint gate widened from `{/v1/chat/completions}` to `{/v1/chat/completions, /v1/embeddings}`. Anything else still 422s as `batch_endpoint_unsupported`.
  - Anthropic + `/v1/embeddings` → 422 `embeddings_batch_unsupported_provider` before reaching the client.
  - Per-line `url` in the serialised JSONL reflects the batch's target endpoint (audit-friendly).
- The polling worker passes `row.endpoint` into `batch_cost_hcents` during finalisation, ensuring embedding batches get embedding pricing applied at the half rate. The existing result parser handles embedding result rows unchanged (no `completion_tokens` field → defaults to 0 via the existing `or 0` fallback).
- Per-sub-request `UsageRecord` rows for embedding batches land with `prompt_tokens > 0`, `completion_tokens = 0`, `status = "batch_success"`, `request_id = "{batch_id}#{custom_id}"`.
- Provider scope (v1):
  - **OpenAI**: supported. `text-embedding-3-small`, `text-embedding-3-large`, `text-embedding-ada-002` (catalog-priced).
  - **Anthropic**: ships no embeddings API; rejected with clear 422.
  - **Cohere / Voyage / Mistral**: ship embeddings APIs but no batches APIs; not supported.
  - **Local sentence-transformers**: not applicable (no upstream to defer to).

### 17. Tool-call result caching (Phase 49)

- Opt-in via per-team `team.tool_result_cache_enabled` (default
  false). When enabled, the gateway memoizes `(team_id, tool_name,
  canonical_args_json) → result` extracted from inbound `tool` role
  messages in chat requests.
- TWO-DIRECTION integration in the chat handler:
  - **Extract**: every `(assistant.tool_calls[i], tool: result)` pair
    in the inbound messages gets its `(name, canonical_args, result)`
    triple written to Redis (per-team TTL, default 1 hour).
  - **Inject**: every trailing `assistant.tool_calls` with no
    matching `tool` follow-up gets each pending call looked up; on
    hit, a synthetic `tool` message is appended to the conversation
    before forwarding to the LLM. Headers
    `X-Pronaos-Tool-Cache-Hits: <N>` +
    `X-Pronaos-Tool-Cache-Tools: <comma-sep names>` surface the
    decision.
- Per-team admin API:
  - `GET  /v1/admin/team/{id}/tool-result-cache` — snapshot
  - `PUT  /v1/admin/team/{id}/tool-result-cache-config` —
    set `enabled` + `ttl_seconds`
  - `DELETE /v1/admin/team/{id}/tool-result-cache` — reset
- Metric `pronaos_tool_result_cache_total{tool_name, result=hit|miss}`
  splits per-tool hit rates.
- Safe ONLY for deterministic-in-args tools. Operator owns the
  policy decision (the gateway exposes the flag, not a per-tool
  allowlist) — tools with side effects (`send_email`,
  `delete_record`) MUST stay uncached.
- Fail-open: Redis outage → silent passthrough (record + lookup
  both no-op); feature degrades to "client must re-execute the
  tool" — never breaks chat.

## Data model (current)

```
tenants         (id, name, created_at, webhook_url, webhook_secret,
                 oidc_subject)
teams           (id, tenant_id, name, monthly_token_budget,
                 current_period_tokens, monthly_cost_hcents_budget,
                 current_period_cost_hcents, period_resets_at,
                 guardrail_policy, allowed_models, routing_strategy,
                 quality_threshold, quality_scores,
                 hedge_delay_ms, hedge_max_count,
                 ab_test,
                 agent_turn_budget_tokens, agent_turn_budget_cost_hcents,
                 agent_turn_ttl_seconds)
api_keys        (id, team_id, prefix, key_hash, scopes, label,
                 created_at, revoked_at, last_used_at, rps_limit)
usage_records   (id, ts, tenant_id, team_id, key_id, provider, model,
                 prompt_tokens, completion_tokens, cost_hcents,
                 request_id, status, ab_arm)
audit_records   (id, ts, tenant_id, team_id, key_id, provider, model,
                 request_id, request_hash, response_hash,
                 prev_hash, this_hash)
```

`usage_records` and `audit_records` are intentionally **not**
foreign-keyed to tenants/teams/keys — when a tenant is deleted we
still want their historical spend and audit trail preserved for
compliance and finance. Indexed `(tenant_id, ts)` and `(team_id, ts)`
for the two hottest query shapes.

25 Alembic migrations apply cleanly from an empty DB (`0001` initial
auth schema → `0025` batch jobs + `team.batches_enabled`).

## Deployment shape

- One stateless `pronaos` container, horizontally scalable behind a
  load balancer.
- Postgres for persistence (SQLite in dev).
- Redis for L1 cache and (optionally) the rate limiter; Qdrant for
  the L2 semantic cache.
- OTEL collector + Prometheus / Tempo / Grafana for the observability
  pipeline. `docker compose up -d` brings up the full stack.

Helm chart + Terraform module are on the roadmap, not shipped.

### Admin REST surface (per phase)

| Phase | Path prefix | Endpoints | Scope |
| --- | --- | --- | --- |
| 5+ | `/v1/admin/usage` + per-team configs (guardrail-policy, allowed-models, routing-strategy, prompt-cache, reasoning, tool-result-cache, hedge-policy, tool-budgets, pii-tokenization, structured-output, quality-monitor, tool-use-scores) + per-tenant webhooks | 32 endpoints | `admin:usage` |
| 63 | `/v1/admin/{tenants,teams,keys}` — full CRUD for identity primitives + generate-once secret | 12 endpoints | `admin:identity` (NEW) |
| 64 | `/v1/admin/budgets/{team_id}` (GET + PUT), `/v1/admin/usage/timeseries` (dense buckets, Python-side bucketing) | 3 endpoints | `admin:usage` for GETs; `admin:identity` for PUT |
| 65 | `/v1/admin/models` — enumerate catalog (anthropic native + CATALOG) annotated with `provider_configured` + `allowed`, bucket-sorted for stable dropdowns | 1 endpoint | `admin:usage` |
| 66 | `/v1/admin/routing/{team_id}` (GET + PUT) — composed per-team routing config (strategy + allowlist + 6 thresholds + 2 score dicts), PATCH semantics with `model_fields_set` | 2 endpoints | `admin:usage` for GET; `admin:identity` for PUT |
| 67 | `/v1/admin/security/{team_id}` (GET + PUT) — composed per-team security config (guardrail_policy + PII tokenization). `/v1/admin/audit/{tenant_id}` (list + verify) — hash-chained audit log viewer + tamper-detection | 4 endpoints | `admin:usage` for reads + verify; `admin:identity` for security PUT |
| 68 | `/v1/admin/providers` (list with circuit state), `/v1/admin/providers/{name}/reset-breaker` (force-reset), `/v1/admin/doctor` (run 14-gate health check) | 3 endpoints | `admin:usage` for GETs; `admin:identity` for reset-breaker |
| 69 | `/v1/admin/batches` (paginated list with filters), `/v1/admin/batches/{id}` (get any team's batch), `/v1/admin/batches/{id}/cancel` (force-cancel) | 3 endpoints | `admin:usage` for GETs; `admin:identity` for cancel |
| 70 | `/v1/admin/webhooks/{tenant_id}` (GET + PUT) — cross-tenant webhook config; `/v1/admin/webhooks/{tenant_id}/test` (fire signed test ping + return result) | 3 endpoints | `admin:usage` for GET; `admin:identity` for PUT + test |
| 71 | `/v1/admin/settings` (GET) — sanitised gateway config snapshot; `PATCH /v1/admin/tenants/{id}` extended to accept `oidc_subject` | 2 endpoints | `admin:usage` for GET; `admin:identity` for OIDC PATCH |

Phase 63's `admin:identity` scope is deliberately distinct from `admin:usage` so a team that needs the FinOps dashboard isn't accidentally granted key-issuance power. Keys with both scopes are common for the operator; keys with only `admin:usage` keep working unchanged. Phase 64 reuses the same split: a key that can read the FinOps dashboard cannot edit caps unless it also carries `admin:identity` — finance stakeholders get read-only by default.

### Admin UI (Phase 62 onward)

Pronaos ships a Next.js 15 admin UI at `web/`. Architecture:

- **Stack**: Next.js App Router + TypeScript (strict + noUncheckedIndexedAccess) + Tailwind + shadcn/ui (new-york preset) + next-themes (light/dark/system) + sonner toasts + React error boundary.
- **Auth**: API-key bearer in localStorage. Same key the user already uses with curl / SDK. Trade-off: XSS-exfiltrable; mitigated by strict CSP (Phase 71) and the fact that the same key already lives in client `.env` files. An opt-in BFF-cookie flow lands in Phase 71 for deployments where the trade-off isn't acceptable.
- **Routes**:
  - `(auth)/login/page.tsx` — paste API key + probe `/v1/healthz` + `/v1/admin/usage` for liveness + scope.
  - `(app)/page.tsx` — dashboard landing. Phase 62 shipped connectivity tiles; **Phase 64** replaces them with real FinOps — 30-day spend / tokens / calls tiles, daily-spend line chart fed by `/v1/admin/usage/timeseries`, and top-5-teams-by-spend table.
  - `(app)/usage/page.tsx` — window-filtered (24h/7d/30d) + team-filtered usage view with bar chart + per-call table. Phase 64.
  - `(app)/usage/budgets/page.tsx` — per-team budget editor with progress meters, days-until-reset countdown, and PUT round-trip. Phase 64.
  - `(app)/playground/page.tsx` — three-column chat playground (params / conversation / inspector). Streams SSE deltas via a custom `streamChatCompletion()` generator; non-streaming toggle uses the same endpoint with `stream=false`. Response inspector surfaces seven `X-Pronaos-*` headers + client TTFT/total. Phase 65.
  - `(app)/routing/page.tsx` — team picker + 7 strategy radio cards + allowlist checkboxes + quality/tool-use score tables (inline edit) + 6 threshold inputs. Every section writes through `PUT /v1/admin/routing/{team_id}` with PATCH semantics. Phase 66.
  - `(app)/guardrails/page.tsx` — team picker + per-rule action editor (7 known rules × 4 actions: block/redact/tokenize/log_only) + PII tokenization toggle + TTL input. Phase 67.
  - `(app)/guardrails/audit/page.tsx` — tenant picker + paginated audit records (prev_hash → this_hash linkage visible) + "Verify chain" button surfacing pass/fail verdict with break details. Phase 67.
  - `(app)/providers/page.tsx` — catalog table with configured flag + p50 latency + colour-coded circuit-state badge + per-row "Reset breaker" CTA. Phase 68.
  - `(app)/doctor/page.tsx` — "Run health check" button + 4 summary tiles + overall verdict banner + gate results grouped by dotted prefix. Phase 68.
  - `(app)/batches/page.tsx` — paginated cross-team batch list with status filter + team filter + colour-coded status badges. Phase 69.
  - `(app)/batches/[id]/page.tsx` — per-batch detail (status, provider, endpoint, request counts, timeline) + Cancel CTA for non-terminal batches. Phase 69.
  - `(app)/webhooks/page.tsx` — tenant picker + URL+secret form + Save/Clear + test-ping card (HTTP status badge + HMAC-signed badge + response body). Phase 70.
  - `(app)/settings/page.tsx` — gateway config cards (11 features, enabled/disabled badges) + per-tenant OIDC subject editor. Phase 71 — **closes the Phase 62–71 UI arc**.
  - `(app)/layout.tsx` — wraps every authenticated page in the AppShell (top nav + side nav + auth gate that redirects to /login when unauthenticated).
- **Contract validation**: every admin REST response runs through a Zod schema in `web/src/lib/api/schemas.ts` that mirrors the backend Pydantic model in `src/pronaos/api/v1/admin.py`. Mismatches surface as immediate ZodError → toast, not silent type coercion.
- **Dev workflow**: `cd web && npm run dev` on :3000 with Next.js rewrites proxying `/v1/*` to FastAPI on :8000. Single-origin from the browser's view — no CORS.
- **Prod deployment**: `next build` produces a standalone export at `web/out/`. The FastAPI process (`src/pronaos/main.py::_mount_admin_ui`) conditionally mounts that directory at `/admin/*` when present, with a SPA fallback for client-side routes. One-container deploy. When `web/out/` is absent (dev), the mount is silently skipped so the dev workflow keeps working.
- **Phase plan**: 62 (foundation) → 63 (identity) → 64 (FinOps) → 65 (playground) → 66 (routing) → 67 (security + audit) → 68 (reliability) → 69 (batches) → 70 (webhooks) → **71 (settings + polish — DONE)**. **The Phase 62–71 UI arc is complete.** Every new backend feature from this point co-ships its UI counterpart in the same phase.
- **Tests**: Playwright e2e at `web/tests/e2e/*.spec.ts`. Mocked-backend; covers auth flow + dashboard render + error states. Python-side contract probe at `scripts/verify_ui_foundation.py` runs the gateway in-process and asserts response shapes match the UI's Zod expectations.

### Operator health check (`pronaos-cli doctor`, Phase 61)

A one-shot diagnostic that an operator runs after `db upgrade` /
first deploy / post-incident. 14 independent gates run in order:

- **Config (2)**: `secret_key` length, `database_url` parseability
- **DB (3)**: connect + alembic_version at head + 5 core tables
  (tenants, teams, api_keys, usage_records, batches) present
- **Auth seed (3)**: ≥ 1 tenant / team / active API key — WARN if
  empty (gateway boots but every chat call 401s)
- **Optional backends (2)**: `redis.ping` (SKIP when unset, PASS
  on PONG); `qdrant.reachable` (SKIP when semantic cache off,
  HTTP-probe otherwise)
- **Provider catalog (1)**: ≥ 1 `settings_attr` populated
- **Optional features (3)**: OIDC discovery URL fetchable, MCP SDK
  importable, batches worker importable

Verdict taxonomy: `PASS` / `FAIL` / `WARN` / `SKIP`. Every gate runs
even if earlier ones failed; runner catches gate-internal exceptions
and reports them as FAILs rather than crashing. Exit code 0 on no-
FAILs (WARN/SKIP allowed); `--strict` flips WARN → FAIL for CI
gating. `--probe-providers` opts into a GET `/v1/models` per
configured provider (no tokens spent — verifies auth, not chat).
`--json` emits a stable schema for piping into `jq`.

Empirical claim #48 (`scripts/verify_doctor.py`) verifies the
healthy-vs-broken distinction across 12 assertions: scenario A
(healthy seeded gateway) hits 10 PASS / 0 FAIL / 0 WARN / 4 SKIP +
exit 0; scenario B (no tenant) hits 3 specific WARNs + exit 0
lenient + exit 1 strict; gate count stable at 14 across both.

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
| Runaway agent loop               | Per-turn budget gate denies the call that would exceed `agent_turn_budget_tokens` (HTTP 429) |

## What's not built

Items listed elsewhere as `🔜 roadmap` and the reason they're deferred:

- **Admin UI (Next.js)** — shipped in Phases 62–71 (Claims 49–58); 17 pages across identity, FinOps, playground, routing, guardrails, reliability, batches, webhooks, and settings.
- **SCIM provisioning** — Phase 26 ships JWT-based OIDC admin auth
  (any standards-compliant IdP). Auto-provisioning users +
  groups via SCIM is the next layer for large-IT-team workflows;
  not yet built.
- **Helm + Terraform** — `docker compose up -d` is the current
  one-command path; production-grade deploy is a packaging task.
- **Cross-replica HALF_OPEN single-probe lock** — Phase 25 shares
  trip state across replicas, but in HALF_OPEN every replica can
  currently send its own probe (first to record wins, matching the
  per-process behaviour). A single-probe lock — only one replica
  probes; others wait — would save a few extra upstream calls in
  the recovery window. Useful at very high replica count.
- **Anthropic native streaming tool_use live verify** — implemented
  + unit-tested with realistic SSE bodies via `respx`. Needs a real
  Anthropic key for end-to-end demo against the actual API.
- **Per-team Presidio entity / min_score overrides** — the policy
  validator accepts `presidio.{entities,min_score}` keys today, but
  only the `enabled` shorthand is honoured at request time. Per-team
  entity-list and threshold overrides are a small follow-up.
- **Periodic auto-eval scheduler** — Phase 24 wires per-model eval
  scores into the router via a CLI bridge. An opt-in scheduler that
  re-runs the eval on a cadence (cron-style) and refreshes
  `team.quality_scores` automatically is a polish step.
- **Bedrock + Vertex native adapters** — shipped in Phases 42/52 (AWS Bedrock, Claim 29/39) and Phase 53 (Google Vertex AI, Claim 40).
