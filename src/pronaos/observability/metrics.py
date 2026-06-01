"""Prometheus metrics for the gateway.

Conventions
-----------
- Metric names use the ``pronaos_`` prefix so they're trivially filterable
  in a multi-tenant Prometheus.
- We use a **dedicated registry** rather than ``REGISTRY`` (the default
  module-level singleton) so tests can construct/destroy state cleanly
  without ``Duplicated timeseries`` errors when the module re-imports.
- Histogram buckets are tuned for the gateway's expected latency profile
  (5 ms to 30 s — anything past 30 s is timed out by the failover layer
  before metrics land).

Cardinality
-----------
Labels are deliberately conservative:

- ``tenant_id`` / ``team_id`` / ``key_id`` are **not** on hot-path counters
  (HTTP requests, provider calls) — a tenant/team explosion would balloon
  series count. FinOps queries reach for the ``usage_records`` table
  instead, which is authoritative.
- ``provider`` / ``model`` are bounded by the catalog (~12 providers, dozens
  of models) so they're safe as labels.
- ``status_code`` / ``status`` are tiny enumerated sets.

This keeps the working-set series count in the low hundreds for a typical
deployment regardless of customer count.
"""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

# A dedicated registry — see module docstring for rationale.
REGISTRY = CollectorRegistry()


# --------------------------------------------------------------------------- #
# HTTP-level metrics                                                          #
# --------------------------------------------------------------------------- #

http_requests_total = Counter(
    "pronaos_http_requests_total",
    "Total HTTP requests served by the gateway.",
    labelnames=("method", "route", "status_code"),
    registry=REGISTRY,
)

# Histogram bucket choices reflect "5 ms cache hit … 30 s p99 cold provider
# call." Tighter low-end buckets matter more than the upper tail because
# anything north of 30 s is already an SLA breach worth alerting on.
http_request_duration_seconds = Histogram(
    "pronaos_http_request_duration_seconds",
    "End-to-end HTTP request duration in seconds.",
    labelnames=("method", "route"),
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
    registry=REGISTRY,
)


# --------------------------------------------------------------------------- #
# Provider-call metrics                                                       #
# --------------------------------------------------------------------------- #

provider_requests_total = Counter(
    "pronaos_provider_requests_total",
    "Upstream provider calls made by the gateway. ``status`` is success|error.",
    labelnames=("provider", "model", "status"),
    registry=REGISTRY,
)

provider_request_duration_seconds = Histogram(
    "pronaos_provider_request_duration_seconds",
    "Duration of a successful upstream provider call, seconds.",
    labelnames=("provider", "model"),
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 20.0, 30.0, 60.0),
    registry=REGISTRY,
)

provider_tokens_total = Counter(
    "pronaos_provider_tokens_total",
    "Tokens billed by providers. ``direction`` is prompt|completion.",
    labelnames=("provider", "model", "direction"),
    registry=REGISTRY,
)

provider_cost_hcents_total = Counter(
    "pronaos_provider_cost_hcents_total",
    "Cumulative cost in hundredths-of-a-cent (matches usage_records.cost_hcents).",
    labelnames=("provider", "model"),
    registry=REGISTRY,
)


# --------------------------------------------------------------------------- #
# Quota / rate-limit metrics                                                  #
# --------------------------------------------------------------------------- #

quota_denials_total = Counter(
    "pronaos_quota_denials_total",
    "Requests denied at the quota gate. ``reason`` distinguishes rate-limit "
    "from per-budget exhaustion.",
    labelnames=("reason",),
    registry=REGISTRY,
)


# --------------------------------------------------------------------------- #
# Cache metrics (Phase 7)                                                     #
# --------------------------------------------------------------------------- #

cache_lookups_total = Counter(
    "pronaos_cache_lookups_total",
    "Cache lookups. ``tier`` is exact|semantic, ``result`` is hit|miss|skip. "
    "``skip`` covers requests bypassed because of temperature>0, streaming, "
    "or an explicit bypass header.",
    labelnames=("tier", "result"),
    registry=REGISTRY,
)


# --------------------------------------------------------------------------- #
# Guardrail metrics (Phase 8)                                                 #
# --------------------------------------------------------------------------- #

guardrail_hits_total = Counter(
    "pronaos_guardrail_hits_total",
    "Guardrail rule firings. ``rule`` is the canonical rule name "
    "(e.g. pii.email, pii.ssn, injection); ``action`` is the action "
    "applied (block | redact | log_only); ``direction`` is ingress|egress.",
    labelnames=("rule", "action", "direction"),
    registry=REGISTRY,
)


# --------------------------------------------------------------------------- #
# Circuit-breaker metrics (Phase 15)                                          #
# --------------------------------------------------------------------------- #
#
# The breaker has cross-request state: dashboards want to read the *current*
# state (a gauge), and FinOps wants to count *events* — trips and the
# upstream calls those trips saved (counters). Three series total, each
# labelled by provider so a Grafana panel can split by upstream.

circuit_state = Gauge(
    "pronaos_circuit_state",
    "Current circuit breaker state per provider. "
    "0=closed (healthy), 1=half_open (probing), 2=open (tripped).",
    labelnames=("provider",),
    registry=REGISTRY,
)

circuit_trips_total = Counter(
    "pronaos_circuit_trips_total",
    "Number of times the circuit transitioned from CLOSED/HALF_OPEN to OPEN. "
    "A trip is a discrete event — a long outage adds 1, not many.",
    labelnames=("provider",),
    registry=REGISTRY,
)

circuit_skipped_requests_total = Counter(
    "pronaos_circuit_skipped_requests_total",
    "Provider attempts skipped because the breaker was OPEN. "
    "This measures the *value* of the breaker — upstream calls saved.",
    labelnames=("provider",),
    registry=REGISTRY,
)


# --------------------------------------------------------------------------- #
# Streaming-cancellation metric (Phase 18)                                    #
# --------------------------------------------------------------------------- #
#
# A "cancelled" stream is one the client tore down before the upstream
# provider finished. Each tick represents one real-world cost-saving
# opportunity (the upstream connection was closed mid-response). Useful for
# capacity planning ("what fraction of our streams are cancelled?") and
# for alerting on a spike that suggests a downstream client bug.

streams_cancelled_total = Counter(
    "pronaos_streams_cancelled_total",
    "Streaming responses cancelled by the client mid-stream. Measured at "
    "the gateway's outbound generator; counts one per cancellation event.",
    labelnames=("provider", "model"),
    registry=REGISTRY,
)


# --------------------------------------------------------------------------- #
# Pre-flight quota gate (Phase 20)                                            #
# --------------------------------------------------------------------------- #
#
# Pre-flight denials are quota rejections issued BEFORE the upstream provider
# call — based on a heuristic token estimate vs the team's remaining budget.
# They save real money (a denied-anyway request never hits Groq/Anthropic).
# Distinguishing pre- vs post-flight denials in the counter lets dashboards
# answer "how many upstream calls did the preflight gate save?"

preflight_denials_total = Counter(
    "pronaos_preflight_denials_total",
    "Requests denied before the upstream call because the estimated total "
    "tokens (prompt + max_completion) exceeded the team's remaining budget. "
    "``reason`` is monthly_token_budget_exhausted or "
    "monthly_cost_budget_exhausted, matching the post-flight denial labels.",
    labelnames=("reason",),
    registry=REGISTRY,
)


# --------------------------------------------------------------------------- #
# Cost-aware routing (Phase 21)                                               #
# --------------------------------------------------------------------------- #
# Tracks every ``model="auto"`` resolution: which strategy picked which
# concrete model. ``selected_model`` is the full ``provider/model`` form
# so dashboards can answer "what does cheapest pick most often?" Cardinality
# is bounded by the catalog * strategies (~3 * ~25 = 75 series tops).

routing_decisions_total = Counter(
    "pronaos_routing_decisions_total",
    "Auto-routing decisions made when a client sent model='auto'. "
    "``strategy`` is the team's routing_strategy; ``selected_model`` is "
    "the concrete provider/model the scorer picked.",
    labelnames=("strategy", "selected_model"),
    registry=REGISTRY,
)


# --------------------------------------------------------------------------- #
# Request hedging (Phase 27)                                                  #
# --------------------------------------------------------------------------- #
# Hedging fires speculatively when the primary doesn't return within
# ``hedge_delay_ms``. Three counters tell the full story:
#
# - ``triggered`` — how often the hedge was started (after the delay
#   elapsed without primary completion). The denominator for "what
#   fraction of requests we hedged."
# - ``wins`` — which side won the race per provider. ``role`` is
#   ``primary`` if the original beat the hedge, ``hedge`` if the
#   speculative call beat the primary.
# - ``cancelled`` — the loser's provider, so dashboards can compute
#   the upstream-call overhead honestly ("we paid for N cancelled
#   calls to provider X this hour").

hedge_triggered_total = Counter(
    "pronaos_hedge_triggered_total",
    "Times the hedge timer elapsed and a speculative provider call was "
    "started. ``primary`` is the original target; ``hedge`` is the "
    "alternative the executor raced it against.",
    labelnames=("primary", "hedge"),
    registry=REGISTRY,
)

hedge_wins_total = Counter(
    "pronaos_hedge_wins_total",
    "Winning provider in a hedged race. ``role`` is ``primary`` if the "
    "original beat the hedge, or ``hedge`` if the speculative call won.",
    labelnames=("winner_provider", "role"),
    registry=REGISTRY,
)

hedge_cancelled_total = Counter(
    "pronaos_hedge_cancelled_total",
    "Provider calls cancelled mid-flight because the racing partner won. "
    "Counts the wasted upstream attempts — multiply by mean tokens for "
    "a rough cost-overhead estimate.",
    labelnames=("cancelled_provider",),
    registry=REGISTRY,
)


# --------------------------------------------------------------------------- #
# Streaming cache replay (Phase 28)                                           #
# --------------------------------------------------------------------------- #
# Counts how often a streaming chat call was served entirely from cache —
# zero upstream tokens consumed, response replayed as SSE at the original
# cadence. Distinct from the generic ``cache_lookups_total{result="hit"}``
# counter because streaming hits carry an extra UX dimension (latency-to-
# first-token, cadence fidelity) and operators may want to alert on a
# regression in replay coverage independently of L1/L2 hit-rate.

cache_stream_replays_total = Counter(
    "pronaos_cache_stream_replays_total",
    "Streaming chat responses served from cache (replayed as SSE). "
    "``tier`` distinguishes L1 (exact) vs L2 (semantic) hits.",
    labelnames=("tier",),
    registry=REGISTRY,
)


# --------------------------------------------------------------------------- #
# A/B testing (Phase 29)                                                      #
# --------------------------------------------------------------------------- #
# One counter per arm assignment. ``test_id`` is the per-test UUID;
# ``arm`` is the letter (a / b) the request was bucketed into. Bounded
# cardinality: at most a few active tests per gateway, two arms each.

ab_decisions_total = Counter(
    "pronaos_ab_decisions_total",
    "A/B test arm assignments. One tick per request that the harness substituted a model on.",
    labelnames=("test_id", "arm"),
    registry=REGISTRY,
)


# --------------------------------------------------------------------------- #
# Agent-turn budget gate (Phase 30)                                           #
# --------------------------------------------------------------------------- #
# Counts per-execution agent-turn budget denials, split by reason
# (token-budget vs cost-budget exhaustion). High deny rate signals
# either too-tight gate budgets or genuinely runaway agents — either
# way the operator wants to see it.

agent_turn_denials_total = Counter(
    "pronaos_agent_turn_denials_total",
    "Per-call denials at the agent-turn budget gate (Phase 30). "
    "``reason`` is agent_turn_token_budget_exhausted or "
    "agent_turn_cost_budget_exhausted.",
    labelnames=("reason",),
    registry=REGISTRY,
)


# --------------------------------------------------------------------------- #
# Embedding endpoint (Phase 31)                                               #
# --------------------------------------------------------------------------- #
# Counts and timing for /v1/embeddings calls. Same dimensional shape as the
# chat-side provider counters so dashboards can compose chat + embedding
# spend in one query.

embedding_requests_total = Counter(
    "pronaos_embedding_requests_total",
    "Embedding requests served by the gateway. ``status`` is success|error.",
    labelnames=("provider", "model", "status"),
    registry=REGISTRY,
)

embedding_request_duration_seconds = Histogram(
    "pronaos_embedding_request_duration_seconds",
    "Duration of a successful embedding call, seconds (provider call only, "
    "excludes cache-hit fast path).",
    labelnames=("provider", "model"),
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
    registry=REGISTRY,
)

embedding_tokens_total = Counter(
    "pronaos_embedding_tokens_total",
    "Input tokens consumed by embedding requests (no completion tokens — "
    "the response is a vector, not text).",
    labelnames=("provider", "model"),
    registry=REGISTRY,
)

embedding_cache_hits_total = Counter(
    "pronaos_embedding_cache_hits_total",
    "Embedding requests served entirely from cache — zero upstream tokens, "
    "zero upstream cost. Labelled by model so dashboards can show per-model "
    "cache effectiveness for FinOps reporting.",
    labelnames=("model",),
    registry=REGISTRY,
)


# --------------------------------------------------------------------------- #
# Rerank endpoint (Phase 32)                                                  #
# --------------------------------------------------------------------------- #
# Counts and timing for /v1/rerank calls. Same dimensional shape as the
# chat-side provider counters so dashboards can compose chat + embedding +
# rerank spend in one query.

rerank_requests_total = Counter(
    "pronaos_rerank_requests_total",
    "Rerank requests served by the gateway. ``status`` is success|error.",
    labelnames=("provider", "model", "status"),
    registry=REGISTRY,
)

rerank_request_duration_seconds = Histogram(
    "pronaos_rerank_request_duration_seconds",
    "Duration of a successful rerank call, seconds (provider call only, "
    "excludes cache-hit fast path).",
    labelnames=("provider", "model"),
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
    registry=REGISTRY,
)

rerank_cache_hits_total = Counter(
    "pronaos_rerank_cache_hits_total",
    "Rerank requests served entirely from cache — zero upstream cost. "
    "Labelled by model so dashboards can show per-model cache effectiveness.",
    labelnames=("model",),
    registry=REGISTRY,
)


# --------------------------------------------------------------------------- #
# Singleflight dedup (Phase 33)                                               #
# --------------------------------------------------------------------------- #
# Counts requests that joined an in-flight leader rather than firing their
# own upstream call. High follower rate = bursty identical-input workload =
# big savings. Flat counter = no concurrent duplicates = singleflight not
# active. Either way the operator can read off the dedup effectiveness.

singleflight_followers_total = Counter(
    "pronaos_singleflight_followers_total",
    "Requests that became singleflight followers (joined an in-flight "
    "leader instead of making their own upstream call). Each follower "
    "represents one saved upstream invocation.",
    labelnames=("endpoint",),
    registry=REGISTRY,
)


# --------------------------------------------------------------------------- #
# Prompt-cache token attribution (Phase 34)                                   #
# --------------------------------------------------------------------------- #
# Anthropic prompt caching gives ~90% cost reduction on cache hits. The
# provider returns cache_creation_input_tokens (writing the cache, billed
# at 1.25x) and cache_read_input_tokens (reading the cache, billed at
# 0.10x). Pronaos surfaces both as separate counters so dashboards can
# show "how many tokens were served from cache" vs "how many were created"
# — the savings story.

prompt_cache_tokens_total = Counter(
    "pronaos_prompt_cache_tokens_total",
    "Anthropic prompt-cache token counts. ``type`` is read (cache hit, "
    "0.10x cost) or write (cache creation, 1.25x cost). Sum over read = "
    "tokens NOT charged at regular input price; the headline FinOps win.",
    labelnames=("provider", "model", "type"),
    registry=REGISTRY,
)


# --------------------------------------------------------------------------- #
# Reasoning-token metric (Phase 56)                                           #
# --------------------------------------------------------------------------- #
# Extended-thinking / reasoning-mode tokens across providers. ``source``
# discriminates the wire field the count came from so dashboards can
# separate the provider-reported counts (OpenAI o1/o3, DeepSeek R1,
# Gemini thoughtsTokenCount) from Pronaos's char-length estimate for
# Anthropic (which doesn't expose a separate count).

reasoning_tokens_total = Counter(
    "pronaos_reasoning_tokens_total",
    "Reasoning / extended-thinking token counts. ``source`` is "
    "``upstream`` (count from the provider's usage block — exact) or "
    "``estimated`` (Pronaos-estimated from thinking-block char length, "
    "Anthropic only). Sum over a (provider, model) split shows the "
    "reasoning-mode FinOps weight for that model.",
    labelnames=("provider", "model", "source"),
    registry=REGISTRY,
)


# --------------------------------------------------------------------------- #
# Tool-call observability (Phase 37)                                          #
# --------------------------------------------------------------------------- #
# Per-tool metrics for agent workloads. The LLM emits tool_calls; Pronaos
# observes the names and counts them. ``status`` distinguishes calls the
# upstream actually saw (emitted) from calls that would have happened if
# the tool weren't strip-by-removal'd from the request's tools array
# (stripped). Sum over emitted = total tool invocations the team's agent
# triggered; sum over stripped = how many budget denials we enforced.

tool_calls_total = Counter(
    "pronaos_tool_calls_total",
    "Tool-call observability. ``tool_name`` is the function name "
    "(e.g. web_search, code_exec). ``status`` is ``emitted`` (LLM "
    "actually called the tool in its response) or ``stripped`` (the "
    "tool was removed from the request before reaching the upstream "
    "because the team's per-tool budget for it was exhausted).",
    labelnames=("tool_name", "status"),
    registry=REGISTRY,
)

tool_budget_denials_total = Counter(
    "pronaos_tool_budget_denials_total",
    "Per-tool budget denials (strip-by-removal events). Each tick "
    "represents one tool stripped from one request because the team "
    "has hit its per-tool monthly cap. Operators alert on a sudden "
    "ramp here = a team's agent loop is repeatedly hitting the same "
    "cap and the workload may need re-budgeting.",
    labelnames=("tool_name",),
    registry=REGISTRY,
)

# Phase 49 — tool-call result cache.
tool_result_cache_total = Counter(
    "pronaos_tool_result_cache_total",
    "Tool-call result cache lookups. ``result`` is ``hit`` (the "
    "gateway injected a cached result for the pending tool_call, "
    "saving the client a tool-execution round trip) or ``miss`` "
    "(no cached result for the (tool_name, args) pair). ``tool_name`` "
    "lets dashboards split per-tool hit rates so operators can spot "
    "which tools their workload caches well and which don't.",
    labelnames=("tool_name", "result"),
    registry=REGISTRY,
)

# Phase 54 — MCP client federation. A chat request that references
# external MCP servers via ``body.pronaos_mcp_servers`` triggers a
# multi-turn loop where the gateway dispatches federated tool_calls
# to the right server and injects their results back into the chat.
mcp_federated_tool_calls_total = Counter(
    "pronaos_mcp_federated_tool_calls_total",
    "MCP client-federation tool_call dispatches. ``server`` is the "
    "operator-provided server name (the prefix in "
    "``{server}.{tool}``); ``tool`` is the original tool name; "
    "``result`` is ``ok`` (CallToolResult.isError=False), "
    "``upstream_error`` (server returned isError=True), or "
    "``federation_error`` (server unreachable / not registered / "
    "raised). Dashboards can spot misbehaving federated servers + "
    "per-server cost attribution by joining this metric on the "
    "chat-request request_id.",
    labelnames=("server", "tool", "result"),
    registry=REGISTRY,
)
mcp_federation_sessions_total = Counter(
    "pronaos_mcp_federation_sessions_total",
    "Chat completions that took the MCP client-federation branch. "
    "``result`` is ``ok`` (loop terminated with a final assistant "
    "response), ``max_iterations`` (loop hit the configured cap and "
    "returned the last response anyway), ``invalid_spec`` (request "
    "body had a malformed ``pronaos_mcp_servers`` entry — 422 "
    "returned before any work).",
    labelnames=("result",),
    registry=REGISTRY,
)

# Phase 58 — streaming MCP federation sessions. Ticks alongside
# ``mcp_federation_sessions_total`` whenever the streaming branch
# was taken (``stream=true`` + ``pronaos_mcp_servers``). Splitting
# the counters keeps the existing non-streaming time series intact
# for operators with dashboards keyed on it.
mcp_streaming_federation_sessions_total = Counter(
    "pronaos_mcp_streaming_federation_sessions_total",
    "Chat completions that took the streaming MCP client-federation "
    "branch (``stream=true`` + ``pronaos_mcp_servers`` together). "
    "Same ``result`` taxonomy as ``mcp_federation_sessions_total``. "
    "Sum of both counters under the same ``result`` = total federation "
    "sessions; this one isolates the streaming subset so operators "
    "can compare TTFT / latency between modes.",
    labelnames=("result",),
    registry=REGISTRY,
)


# Phase 59 — async batches API. Ticks every time a batch enters
# (submitted) or exits (completed | failed | expired | cancelled)
# a notable state. ``provider`` ∈ {openai, anthropic}; ``status``
# is the Pronaos-normalized state from ``core/batches.py``. The
# operator can split sync vs batch spend by joining this with
# ``usage_records.is_batch``.
batch_jobs_total = Counter(
    "pronaos_batch_jobs_total",
    "Async-batch state transitions tracked by Pronaos. Submitted "
    "batches tick once with ``status=validating``; the polling "
    "worker ticks again on each terminal-state transition. The "
    "delta between submitted and terminal counts is in-flight.",
    labelnames=("provider", "status"),
    registry=REGISTRY,
)


# Phase 51 — MCP streaming progress notifications.
mcp_streaming_chunks_total = Counter(
    "pronaos_mcp_streaming_chunks_total",
    "MCP tools/call streaming chunks emitted as notifications/progress. "
    "Incremented once per upstream chunk that the MCP server forwarded "
    "as a progress notification to the client. ``transport`` is "
    "``stdio`` or ``sse`` so dashboards can split streaming traffic "
    "by transport. A flat-zero series means clients are not requesting "
    "streaming via the ``_meta.progressToken`` mechanism (or are using "
    "MCP for non-chat tools that don't stream).",
    labelnames=("transport",),
    registry=REGISTRY,
)
mcp_streaming_sessions_total = Counter(
    "pronaos_mcp_streaming_sessions_total",
    "MCP tools/call invocations that took the streaming branch (because "
    "the inbound request carried a ``_meta.progressToken``). ``result`` "
    "is ``ok`` (full streaming completed and the synthesized final "
    "CallToolResult was returned), ``upstream_error`` (loopback HTTP "
    "returned non-200 before any progress notification was sent), or "
    "``mid_stream_error`` (an exception during chunk forwarding cut "
    "the stream short — partial progress notifications were delivered "
    "but the final result is the error payload).",
    labelnames=("transport", "result"),
    registry=REGISTRY,
)


# --------------------------------------------------------------------------- #
# PII tokenization (Phase 38)                                                 #
# --------------------------------------------------------------------------- #
#
# Three counters cover the lifecycle:
# - ``created`` increments when a value is tokenized on ingress (one per
#   unique (rule, value) pair within a request — deduplicated by token).
# - ``reversed`` increments when a token in the upstream response is
#   resolved back to its original via the Redis lookup.
# - ``orphaned`` increments when a token appears in the response but the
#   Redis mapping has expired or never existed (Redis flake, TTL elapsed,
#   or the model hallucinated a token shape). Orphaned tokens stay in
#   the client-facing response — operators alert on a non-trivial
#   ``orphaned`` rate as a Redis health signal.

pii_tokens_created_total = Counter(
    "pronaos_pii_tokens_created_total",
    "PII tokens minted on the ingress path. ``rule`` matches the "
    "guardrail rule suffix (``email``, ``phone``, ``ipv4``, ``name``, "
    "etc). Each tick = one unique (rule, value) pair tokenized in one "
    "request; duplicates within a request collapse to one token (entity "
    "tracking).",
    labelnames=("rule",),
    registry=REGISTRY,
)

pii_tokens_reversed_total = Counter(
    "pronaos_pii_tokens_reversed_total",
    "PII tokens reversed on the egress path. Each tick = one token "
    "occurrence in the response that was successfully resolved back "
    "to its original via Redis. A token mentioned twice in the response "
    "counts twice.",
    labelnames=("rule",),
    registry=REGISTRY,
)

pii_tokens_orphaned_total = Counter(
    "pronaos_pii_tokens_orphaned_total",
    "PII tokens found in the response that did NOT resolve to an "
    "original — Redis lookup miss. Causes: TTL expired between ingress "
    "and egress, Redis outage during MGET, or the model hallucinated "
    "a token-shape string that was never minted. A persistent non-zero "
    "rate is a Redis health alert.",
    labelnames=("rule",),
    registry=REGISTRY,
)


# --------------------------------------------------------------------------- #
# Multi-modal image input (Phase 41)                                          #
# --------------------------------------------------------------------------- #
#
# Three counters cover the image-input lifecycle:
# - ``image_inputs_total`` ticks once per IMAGE part (not per request),
#   so a request with 3 images counts as 3. Labels by provider + model
#   so dashboards can split vision-workload share by underlying model.
# - ``image_bytes_total`` ticks the cumulative base64 payload bytes —
#   useful for capacity planning (Redis, Postgres, audit storage all
#   grow with this number).
# - ``image_rejections_total`` ticks when an image request was REJECTED
#   pre-flight by the size cap. ``reason`` distinguishes too_large,
#   invalid_shape, unsupported_model so dashboards can split user
#   error from operator policy.

image_inputs_total = Counter(
    "pronaos_image_inputs_total",
    "Image parts present in successful chat requests. One tick per "
    "image (a request with N images bumps by N). Labels by provider "
    "+ model for per-route attribution.",
    labelnames=("provider", "model"),
    registry=REGISTRY,
)

image_bytes_total = Counter(
    "pronaos_image_bytes_total",
    "Cumulative base64 image-payload bytes in successful requests. "
    "Useful for storage capacity planning and per-tenant cost "
    "attribution (some providers charge by image data ingress).",
    labelnames=("provider", "model"),
    registry=REGISTRY,
)

image_rejections_total = Counter(
    "pronaos_image_rejections_total",
    "Image-input requests rejected pre-flight. ``reason`` = "
    "``too_large`` (exceeded team.max_image_bytes) | "
    "``invalid_shape`` (malformed multi-modal content) | "
    "``unsupported_model`` (model in allowlist doesn't accept "
    "images). Distinct so operators can split user error from "
    "policy / capability gaps.",
    labelnames=("reason",),
    registry=REGISTRY,
)


# --------------------------------------------------------------------------- #
# Quality regression monitoring (Phase 40)                                    #
# --------------------------------------------------------------------------- #
#
# Three counters cover the lifecycle:
# - ``quality_samples_total{model, result}`` ticks on every sampled
#   response. ``result`` = ``ok`` (judge returned a score) or ``failed``
#   (judge call errored — operational alert if it stays non-zero).
# - ``quality_degradations_total{model, action}`` ticks on every state
#   transition: ``detected`` (model fell below baseline) or
#   ``recovered`` (returned to baseline). The product of these two
#   = the model's reliability story over time.

quality_samples_total = Counter(
    "pronaos_quality_samples_total",
    "Production responses sampled by the quality monitor. ``model`` is "
    "the fqmn being scored. ``result`` is ``ok`` (judge returned a "
    "valid score) or ``failed`` (judge call errored / unparseable "
    "reply). Operators alert when ``failed`` rate climbs — typically "
    "a judge-model outage.",
    labelnames=("model", "result"),
    registry=REGISTRY,
)

quality_degradations_total = Counter(
    "pronaos_quality_degradations_total",
    "Quality-monitor state transitions. ``action`` is ``detected`` "
    "(model crossed below baseline with p < detect_p) or "
    "``recovered`` (model returned to baseline with p > recover_p). "
    "Pair counter for tracking each model's reliability over time.",
    labelnames=("model", "action"),
    registry=REGISTRY,
)


# --------------------------------------------------------------------------- #
# Structured output validation (Phase 39)                                     #
# --------------------------------------------------------------------------- #
#
# Two counters cover the lifecycle:
# - ``schema_validation_total`` increments once per request that carried a
#   JSON Schema. ``result`` = passed (first try clean), retried (passed
#   after at least one retry), or failed (exhausted retries, returning a
#   schema-violating response with the validation-failed header).
# - ``schema_retries_total`` increments once per retry fired. Sum gives
#   the per-team "retry budget consumed" view.

schema_validation_total = Counter(
    "pronaos_schema_validation_total",
    "Outcomes of gateway-side JSON Schema validation. ``result`` is "
    "``passed`` (validated on first response), ``retried`` (validated "
    "after at least one auto-retry — captures the win case), or "
    "``failed`` (exhausted ``structured_output_max_retries``; the "
    "response is still returned to the client with the failed header). "
    "``model`` lets dashboards aggregate by underlying model — useful "
    'for routing decisions ("which model has the lowest schema '
    'violation rate on our workloads").',
    labelnames=("result", "model"),
    registry=REGISTRY,
)

schema_retries_total = Counter(
    "pronaos_schema_retries_total",
    "One tick per retry fired by the validation loop. Operators alert "
    "on a sudden ramp — typically a sign that a specific model has "
    "regressed on structured-output reliability and needs to be "
    "rerouted or removed from the team's allowlist.",
    labelnames=("model",),
    registry=REGISTRY,
)


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def record_provider_success(
    provider: str,
    model: str,
    duration_seconds: float,
    prompt_tokens: int,
    completion_tokens: int,
    cost_hcents: int,
) -> None:
    """Single funnel for the provider counters so the call sites stay readable.

    Keeping this here (rather than inlining four `.labels(...).inc(...)` calls
    in chat.py) means a future re-shape of the labels only changes one file.
    """
    provider_requests_total.labels(provider=provider, model=model, status="success").inc()
    provider_request_duration_seconds.labels(provider=provider, model=model).observe(
        duration_seconds
    )
    if prompt_tokens > 0:
        provider_tokens_total.labels(provider=provider, model=model, direction="prompt").inc(
            prompt_tokens
        )
    if completion_tokens > 0:
        provider_tokens_total.labels(provider=provider, model=model, direction="completion").inc(
            completion_tokens
        )
    if cost_hcents > 0:
        provider_cost_hcents_total.labels(provider=provider, model=model).inc(cost_hcents)


def record_provider_error(provider: str, model: str) -> None:
    provider_requests_total.labels(provider=provider, model=model, status="error").inc()


def record_quota_denial(reason: str) -> None:
    quota_denials_total.labels(reason=reason).inc()


def record_cache_lookup(*, tier: str, result: str) -> None:
    """Record one cache decision. ``tier`` ∈ {exact, semantic}; ``result`` ∈
    {hit, miss, skip}. ``skip`` is its own category so the hit-rate panel
    can compute ``hits / (hits + miss)`` and ignore skip — otherwise
    streaming-heavy traffic would tank the apparent hit rate even when
    the cache is doing its job."""
    cache_lookups_total.labels(tier=tier, result=result).inc()


def record_guardrail_hit(*, rule: str, action: str, direction: str) -> None:
    """Increment the guardrail counter for one rule firing.

    Called once per RuleHit (so multiple emails in one prompt produce
    multiple counter ticks). That's the right granularity for the "PII
    redactions per minute" panel — it matches how dashboards count
    things you'd talk about as "events"."""
    guardrail_hits_total.labels(rule=rule, action=action, direction=direction).inc()


# String→Prometheus-value mapping for the circuit_state gauge. Stable
# numeric encoding so PromQL queries are stable and a Grafana threshold
# panel can colour CLOSED/HALF_OPEN/OPEN consistently.
_CIRCUIT_STATE_VALUES: dict[str, float] = {
    "closed": 0.0,
    "half_open": 1.0,
    "open": 2.0,
}


def record_circuit_state(provider: str, state: str) -> None:
    """Set the circuit-state gauge for ``provider`` from the breaker's
    state string. Called by the registry-snapshot exporter — see
    ``observability/exporter.py`` for the scheduling logic."""
    value = _CIRCUIT_STATE_VALUES.get(state)
    if value is None:
        # Unknown state — refuse silently rather than emit a misleading
        # numeric value that PromQL would mis-colour.
        return
    circuit_state.labels(provider=provider).set(value)


def record_circuit_trip(provider: str) -> None:
    """Bump the trip counter. Called by the failover layer when a
    provider call fails AND the breaker transitions to OPEN — i.e.
    exactly once per trip event."""
    circuit_trips_total.labels(provider=provider).inc()


def record_circuit_skipped(provider: str) -> None:
    """Bump the skipped-requests counter. Called by failover when the
    breaker for ``provider`` was OPEN at request time — measures
    upstream calls the breaker actively saved."""
    circuit_skipped_requests_total.labels(provider=provider).inc()


def record_stream_cancelled(provider: str, model: str) -> None:
    """Bump the streaming-cancellation counter. Called by the chat
    handler's streaming generator when ``CancelledError`` fires —
    i.e. the client closed the connection before the response was
    fully streamed. One tick per cancellation event."""
    streams_cancelled_total.labels(provider=provider, model=model).inc()


def record_preflight_denial(reason: str) -> None:
    """Bump the preflight-denial counter. Called by the chat handler
    when the token estimator + budget check decides this request
    cannot succeed and rejects it before the upstream call.
    ``reason`` mirrors the post-flight denial reasons so dashboards
    can sum across both layers."""
    preflight_denials_total.labels(reason=reason).inc()


def record_routing_decision(*, strategy: str, selected_model: str) -> None:
    """Bump the auto-routing decision counter. Called by the chat
    handler when ``model="auto"`` resolves to a concrete provider/model
    via the cost-aware scorer."""
    routing_decisions_total.labels(strategy=strategy, selected_model=selected_model).inc()


def record_hedge_triggered(*, primary: str, hedge: str) -> None:
    """Bump the hedge-triggered counter. Called by failover when the
    primary fails to return within ``hedge_delay_ms`` and a
    speculative call to ``hedge`` is started."""
    hedge_triggered_total.labels(primary=primary, hedge=hedge).inc()


def record_hedge_win(*, winner_provider: str, role: str) -> None:
    """Bump the hedge-wins counter. ``role`` is ``primary`` if the
    original target beat its hedge, or ``hedge`` if the speculative
    call won. The provider label lets dashboards see which upstream
    "tends to win" hedged races."""
    hedge_wins_total.labels(winner_provider=winner_provider, role=role).inc()


def record_hedge_cancelled(provider: str) -> None:
    """Bump the hedge-cancelled counter. Called for the loser of a
    hedged race so dashboards can quantify the upstream-call overhead
    that hedging costs (one cancelled attempt per hedge that lost)."""
    hedge_cancelled_total.labels(cancelled_provider=provider).inc()


def record_cache_stream_replay(*, tier: str) -> None:
    """Bump the streaming-cache-replay counter. Called by the chat
    handler when a ``stream=true`` request was served entirely from
    cache (Phase 28). ``tier`` ∈ {exact, semantic}."""
    cache_stream_replays_total.labels(tier=tier).inc()


def record_ab_decision(*, test_id: str, arm: str) -> None:
    """Bump the A/B decision counter (Phase 29). One tick per request
    where the harness substituted a model for an arm. ``arm`` ∈ {a, b}."""
    ab_decisions_total.labels(test_id=test_id, arm=arm).inc()


def record_agent_turn_denial(*, reason: str) -> None:
    """Bump the agent-turn denial counter (Phase 30). Called by the
    chat handler when the per-execution budget gate rejects the
    call. ``reason`` ∈ {agent_turn_token_budget_exhausted,
    agent_turn_cost_budget_exhausted}."""
    agent_turn_denials_total.labels(reason=reason).inc()


def record_embedding_success(
    *,
    provider: str,
    model: str,
    duration_seconds: float,
    prompt_tokens: int,
    cost_hcents: int,
) -> None:
    """Single funnel for the embedding counters. Mirrors
    :func:`record_provider_success` but for embedding calls — embeddings
    have no completion tokens, so the shape is narrower."""
    embedding_requests_total.labels(provider=provider, model=model, status="success").inc()
    embedding_request_duration_seconds.labels(provider=provider, model=model).observe(
        duration_seconds
    )
    if prompt_tokens > 0:
        embedding_tokens_total.labels(provider=provider, model=model).inc(prompt_tokens)
    if cost_hcents > 0:
        # Reuse the chat-side provider_cost_hcents_total so FinOps dashboards
        # can sum total spend across chat+embedding in one query.
        provider_cost_hcents_total.labels(provider=provider, model=model).inc(cost_hcents)


def record_embedding_error(*, provider: str, model: str) -> None:
    embedding_requests_total.labels(provider=provider, model=model, status="error").inc()


def record_embedding_cache_hit(*, model: str) -> None:
    """Bump the embedding-cache-hit counter. Called when a /v1/embeddings
    request was served entirely from cache — zero upstream tokens, zero cost."""
    embedding_cache_hits_total.labels(model=model).inc()


def record_rerank_success(
    *,
    provider: str,
    model: str,
    duration_seconds: float,
    prompt_tokens: int,
    cost_hcents: int,
) -> None:
    """Single funnel for the rerank counters. Mirrors :func:`record_embedding_success`
    but for rerank calls."""
    rerank_requests_total.labels(provider=provider, model=model, status="success").inc()
    rerank_request_duration_seconds.labels(provider=provider, model=model).observe(duration_seconds)
    if cost_hcents > 0:
        # Reuse the chat-side provider_cost_hcents_total so FinOps dashboards
        # sum total spend across chat+embedding+rerank in one query.
        provider_cost_hcents_total.labels(provider=provider, model=model).inc(cost_hcents)


def record_rerank_error(*, provider: str, model: str) -> None:
    rerank_requests_total.labels(provider=provider, model=model, status="error").inc()


def record_rerank_cache_hit(*, model: str) -> None:
    """Bump the rerank-cache-hit counter. Called when a /v1/rerank request
    was served entirely from cache — zero upstream cost."""
    rerank_cache_hits_total.labels(model=model).inc()


def record_singleflight_follower(*, endpoint: str) -> None:
    """Bump the singleflight follower counter (Phase 33).

    Called when a request joined an in-flight leader instead of making
    its own upstream call. ``endpoint`` ∈ {chat, embedding, rerank}
    so dashboards can split dedup effectiveness per surface.
    """
    singleflight_followers_total.labels(endpoint=endpoint).inc()


def record_tool_call_emitted(*, tool_name: str) -> None:
    """Bump the tool-call counter for an LLM emission (Phase 37).

    Called once per tool name the LLM emitted in a response's
    ``tool_calls``. Duplicates within the same response are counted
    separately — the LLM may legitimately call the same tool twice
    in one turn, and each is a real invocation.
    """
    tool_calls_total.labels(tool_name=tool_name, status="emitted").inc()


def record_tool_call_stripped(*, tool_name: str) -> None:
    """Bump the tool-call counter for a strip-by-removal event (Phase 37).

    Called once per tool name that was stripped from a request's
    ``tools`` array because the team's per-tool budget for it was
    exhausted. Also increments ``pronaos_tool_budget_denials_total``
    so dashboards can split "what the LLM saw" from "what the operator
    denied" without joining counters.
    """
    tool_calls_total.labels(tool_name=tool_name, status="stripped").inc()
    tool_budget_denials_total.labels(tool_name=tool_name).inc()


def record_tool_result_cache(*, tool_name: str, result: str) -> None:
    """Bump the tool-result cache lookup counter (Phase 49).

    Called once per pending ``tool_call`` checked against the cache.
    ``result`` is ``hit`` when the gateway injected a cached result
    (the client's tool re-execution was skipped) or ``miss`` when
    no cached result was found.
    """
    tool_result_cache_total.labels(tool_name=tool_name, result=result).inc()


def record_mcp_federated_tool_call(*, server: str, tool: str, result: str) -> None:
    """Bump the federated tool-call counter (Phase 54).

    Called once per ``tool_call`` the gateway dispatched to a federated
    MCP server during a chat completion. ``result`` is ``ok`` (call
    returned isError=False), ``upstream_error`` (server's tool returned
    isError=True), or ``federation_error`` (server unreachable / raised
    / not registered).
    """
    mcp_federated_tool_calls_total.labels(server=server, tool=tool, result=result).inc()


def record_mcp_streaming_federation_session(*, result: str) -> None:
    """Phase 58: bump the streaming-federation session counter."""
    mcp_streaming_federation_sessions_total.labels(result=result).inc()


def record_batch_event(*, provider: str, status: str) -> None:
    """Phase 59: bump the async-batches counter on a notable state.

    Called on submit (``status=validating``) and again from the
    polling worker when the batch reaches a terminal state. Operators
    derive in-flight = submitted - terminal across the time series.
    """
    batch_jobs_total.labels(provider=provider, status=status).inc()


def record_mcp_federation_session(*, result: str) -> None:
    """Bump the federation-session counter (Phase 54).

    Called exactly once per chat completion that entered the
    federation branch. ``result`` ∈ {``ok``, ``max_iterations``,
    ``invalid_spec``}.
    """
    mcp_federation_sessions_total.labels(result=result).inc()


def record_mcp_streaming_chunk(*, transport: str) -> None:
    """Bump the MCP streaming-chunk counter (Phase 51).

    Called once per upstream chat-completion chunk forwarded to the
    MCP client as a ``notifications/progress`` message. ``transport``
    is ``stdio`` (subprocess spawned by Claude Code / IDE clients)
    or ``sse`` (remote MCP clients on the SSE transport at
    ``/v1/mcp/sse``).
    """
    mcp_streaming_chunks_total.labels(transport=transport).inc()


def record_mcp_streaming_session(*, transport: str, result: str) -> None:
    """Bump the MCP streaming-session counter (Phase 51).

    Called exactly once per ``tools/call`` that took the streaming
    branch — i.e. carried ``_meta.progressToken``. ``result`` is one
    of ``ok``, ``upstream_error``, ``mid_stream_error`` (see metric
    docstring in the declaration block).
    """
    mcp_streaming_sessions_total.labels(transport=transport, result=result).inc()


def record_quality_sample(*, model: str, result: str) -> None:
    """Bump the quality-sample counter (Phase 40).

    ``result`` ∈ {ok, failed}. ``model`` is the fqmn being judged.
    """
    quality_samples_total.labels(model=model, result=result).inc()


def record_quality_degradation(*, model: str, action: str) -> None:
    """Bump the quality-degradation counter (Phase 40).

    ``action`` ∈ {detected, recovered}. Operators alert on ``detected``
    spikes and dashboard the lag between detected/recovered for each
    model's reliability story.
    """
    quality_degradations_total.labels(model=model, action=action).inc()


def record_image_input(*, provider: str, model: str, count: int = 1, bytes_total: int = 0) -> None:
    """Bump the image-input counters (Phase 41).

    Called after a successful request that contained image parts.
    Distinguishes the image-part count (each image counted once) from
    the payload bytes (cumulative across all images), both labelled by
    ``provider, model`` so dashboards can split vision usage by route.
    """
    if count > 0:
        image_inputs_total.labels(provider=provider, model=model).inc(count)
    if bytes_total > 0:
        image_bytes_total.labels(provider=provider, model=model).inc(bytes_total)


def record_image_rejection(*, reason: str) -> None:
    """Bump the image-rejection counter (Phase 41).

    Called when the chat handler refused a multi-modal request
    pre-flight (size cap, malformed, model lacks capability).
    """
    image_rejections_total.labels(reason=reason).inc()


def record_schema_validation(*, result: str, model: str) -> None:
    """Bump the schema-validation outcome counter (Phase 39).

    ``result`` ∈ {passed, retried, failed}. ``model`` is the fqmn
    (e.g. ``groq/llama-3.1-8b-instant``) — dashboards aggregate by
    model to surface which models have the lowest schema-violation
    rate on the team's workloads.
    """
    schema_validation_total.labels(result=result, model=model).inc()


def record_schema_retry(*, model: str) -> None:
    """Bump the schema-retry counter (Phase 39).

    Called once per retry fired. ``model`` is the fqmn; a sudden ramp
    typically signals a model regression on structured output
    reliability.
    """
    schema_retries_total.labels(model=model).inc()


def record_pii_token_created(*, rule: str, count: int = 1) -> None:
    """Bump the PII tokens-created counter (Phase 38).

    ``rule`` is the short rule suffix (``email``, ``phone``, ``ipv4``,
    ``name``, ``ssn``, ``credit_card``). Called by the chat handler
    after writing the ingress tokenization mapping to Redis.
    """
    if count <= 0:
        return
    pii_tokens_created_total.labels(rule=rule).inc(count)


def record_pii_token_reversed(*, rule: str, count: int = 1) -> None:
    """Bump the PII tokens-reversed counter (Phase 38).

    Called after the egress detokenizer successfully resolved a token
    to its original via Redis.
    """
    if count <= 0:
        return
    pii_tokens_reversed_total.labels(rule=rule).inc(count)


def record_pii_token_orphaned(*, rule: str, count: int = 1) -> None:
    """Bump the PII tokens-orphaned counter (Phase 38).

    Called when a token in the response did NOT resolve — Redis miss
    (TTL expired or Redis flake) or the model hallucinated a token
    that was never minted. A persistent non-zero rate is a Redis
    health signal; operators alert on it.
    """
    if count <= 0:
        return
    pii_tokens_orphaned_total.labels(rule=rule).inc(count)


def record_prompt_cache_tokens(
    *,
    provider: str,
    model: str,
    read_tokens: int,
    write_tokens: int,
) -> None:
    """Bump the prompt-cache token counters (Phase 34).

    Called by the chat handler after every chat completion that returned
    non-zero cache stats (Anthropic prompt caching). Skips the increment
    when both counts are zero so dashboards aren't polluted with no-op
    samples.
    """
    if read_tokens > 0:
        prompt_cache_tokens_total.labels(provider=provider, model=model, type="read").inc(
            read_tokens
        )
    if write_tokens > 0:
        prompt_cache_tokens_total.labels(provider=provider, model=model, type="write").inc(
            write_tokens
        )


def record_reasoning_tokens(
    *,
    provider: str,
    model: str,
    tokens: int,
    source: str,
) -> None:
    """Bump the reasoning-token counter (Phase 56).

    ``source`` is ``upstream`` when the count came from the provider's
    own usage block (OpenAI o1/o3, DeepSeek R1, Gemini
    thoughtsTokenCount — exact) or ``estimated`` when Pronaos derived
    it from Anthropic's thinking-block character length (~4 chars/token).
    Splitting on source lets dashboards distinguish "provider-reported"
    vs "Pronaos-inferred" so operators can read the FinOps signal with
    the right confidence interval.
    """
    if tokens <= 0:
        return
    reasoning_tokens_total.labels(provider=provider, model=model, source=source).inc(tokens)


__all__ = [
    "REGISTRY",
    "ab_decisions_total",
    "agent_turn_denials_total",
    "cache_lookups_total",
    "cache_stream_replays_total",
    "circuit_skipped_requests_total",
    "circuit_state",
    "circuit_trips_total",
    "embedding_cache_hits_total",
    "embedding_request_duration_seconds",
    "embedding_requests_total",
    "embedding_tokens_total",
    "guardrail_hits_total",
    "hedge_cancelled_total",
    "hedge_triggered_total",
    "hedge_wins_total",
    "http_request_duration_seconds",
    "http_requests_total",
    "image_bytes_total",
    "image_inputs_total",
    "image_rejections_total",
    "pii_tokens_created_total",
    "pii_tokens_orphaned_total",
    "pii_tokens_reversed_total",
    "preflight_denials_total",
    "prompt_cache_tokens_total",
    "provider_cost_hcents_total",
    "provider_request_duration_seconds",
    "provider_requests_total",
    "provider_tokens_total",
    "quality_degradations_total",
    "quality_samples_total",
    "quota_denials_total",
    "reasoning_tokens_total",
    "record_ab_decision",
    "record_agent_turn_denial",
    "record_cache_lookup",
    "record_cache_stream_replay",
    "record_circuit_skipped",
    "record_circuit_state",
    "record_circuit_trip",
    "record_embedding_cache_hit",
    "record_embedding_error",
    "record_embedding_success",
    "record_guardrail_hit",
    "record_hedge_cancelled",
    "record_hedge_triggered",
    "record_hedge_win",
    "record_image_input",
    "record_image_rejection",
    "record_pii_token_created",
    "record_pii_token_orphaned",
    "record_pii_token_reversed",
    "record_preflight_denial",
    "record_prompt_cache_tokens",
    "record_provider_error",
    "record_provider_success",
    "record_quality_degradation",
    "record_quality_sample",
    "record_quota_denial",
    "record_reasoning_tokens",
    "record_rerank_cache_hit",
    "record_rerank_error",
    "record_rerank_success",
    "record_routing_decision",
    "record_schema_retry",
    "record_schema_validation",
    "record_singleflight_follower",
    "record_stream_cancelled",
    "record_tool_call_emitted",
    "record_tool_call_stripped",
    "rerank_cache_hits_total",
    "rerank_request_duration_seconds",
    "rerank_requests_total",
    "routing_decisions_total",
    "schema_retries_total",
    "schema_validation_total",
    "singleflight_followers_total",
    "streams_cancelled_total",
    "tool_budget_denials_total",
    "tool_calls_total",
]
