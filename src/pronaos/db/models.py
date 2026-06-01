"""ORM models.

Principles
----------
- **Hash only.** API key secrets never touch the database in plaintext. We
  store an argon2 hash and a short non-secret ``prefix`` for identification.
- **Soft revocation.** ``revoked_at`` is nullable rather than a boolean so we
  keep the audit trail of when a key was killed.
- **UTC timestamps everywhere** — we never rely on server-local time.
- **String IDs.** Using UUIDs (as strings) keeps tenancy leak-safe in URLs
  and works identically on SQLite and Postgres without extension hell.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Shared declarative base for all ORM models."""


def _new_id() -> str:
    return uuid.uuid4().hex


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


def next_period_reset(now: datetime) -> datetime:
    """Return the first day of the month *following* ``now``, at 00:00 UTC.

    Used as the default value for ``Team.period_resets_at``. Calendar-month
    rollover is deliberate over "rolling 30-day" because it aligns with the
    way humans (and most billing systems) reason about monthly limits.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    year, month = now.year, now.month
    if month == 12:
        year += 1
        month = 1
    else:
        month += 1
    return datetime(year, month, 1, 0, 0, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Tenant                                                                      #
# --------------------------------------------------------------------------- #


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    # ---- Webhook config (Phase 19) ----
    # Tenant-wide HTTP webhook for operational events: quota exhaustion,
    # circuit-breaker trips, audit-chain breaks. NULL on either field
    # means "webhooks disabled for this tenant" — the dispatcher is a
    # no-op. The secret is used to HMAC-SHA256-sign every payload so
    # the receiver can verify authenticity (no shared HTTPS cert
    # assumed). Plaintext secret in the DB is acceptable because the
    # gateway already trusts its own DB; rotation is a write to this
    # column. Validated by the CLI / admin endpoint, not the DB.
    webhook_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    webhook_secret: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # ---- OIDC / SSO admin auth (Phase 26) ----
    # When set, an OIDC JWT whose ``sub`` claim equals this value is
    # accepted as admin auth for the tenant — equivalent to a
    # ``admin:usage`` API key. NULL = only the API-key path works for
    # this tenant.
    #
    # The OIDC issuer + audience are gateway-wide settings
    # (``PRONAOS_OIDC_ISSUER`` / ``PRONAOS_OIDC_AUDIENCE``). Per-tenant
    # issuers (multi-IdP-per-deployment) are a future phase.
    oidc_subject: Mapped[str | None] = mapped_column(String(255), nullable=True)

    teams: Mapped[list[Team]] = relationship(
        "Team", back_populates="tenant", cascade="all, delete-orphan"
    )


# --------------------------------------------------------------------------- #
# Team                                                                        #
# --------------------------------------------------------------------------- #


class Team(Base):
    __tablename__ = "teams"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    tenant_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    # ---- Quota fields (Phase 4) ----
    # NULL means "unlimited" — admins must explicitly set a budget. Avoids
    # accidentally denying every request the moment a team is created.
    monthly_token_budget: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, default=None
    )
    # Running counter of tokens consumed in the current billing period.
    # Atomically incremented by QuotaTracker after each successful provider call.
    current_period_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    # ---- Cost budget (Phase 5.7) ----
    # Parallel to the token budget but denominated in hundredths-of-a-cent
    # (matches UsageRecord.cost_hcents). NULL means unlimited. Either limit
    # can deny independently; the stricter one wins.
    monthly_cost_hcents_budget: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, default=None
    )
    current_period_cost_hcents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    # When the period counters auto-reset. Calendar-month UTC: first day
    # of the next month at 00:00 UTC. QuotaTracker handles the rollover on
    # the first request past this timestamp — both token and cost counters
    # zero together.
    period_resets_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: next_period_reset(_utcnow()),
    )

    # ---- Guardrail policy (Phase 8.2) ----
    # Per-team override for the gateway-wide guardrail rules. JSON shape:
    #   {
    #     "disabled_rules": ["pii.ipv4"],
    #     "rule_actions": {"injection": "block"}
    #   }
    # NULL = use engine defaults. ``disabled_rules`` skips the rule
    # entirely (don't even scan); ``rule_actions`` overrides the action
    # for a rule that DOES fire. Validated by the CLI / admin endpoint
    # before write, not by the DB.
    guardrail_policy: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True, default=None
    )

    # ---- Model allowlist (Phase 17) ----
    # Per-team allowlist of model patterns this team's API keys may
    # invoke. JSON list shape:
    #   ["groq/*", "anthropic/claude-opus-*"]
    # Patterns are matched with fnmatch semantics against the request's
    # ``model`` field. NULL = unrestricted (backwards-compat for teams
    # provisioned before this feature shipped). An empty list ``[]``
    # explicitly denies everything — useful for paused teams. Validated
    # by the CLI / admin endpoint before write, not by the DB.
    allowed_models: Mapped[list[str] | None] = mapped_column(JSON, nullable=True, default=None)

    # ---- Routing strategy (Phase 21 / extended Phase 24) ----
    # Selects how ``model="auto"`` requests are resolved to a concrete
    # provider/model. One of ``cheapest`` | ``fastest`` | ``balanced``
    # | ``quality-aware-cheapest`` (the wire format of
    # ``RoutingStrategy``). NULL = no preference; the gateway falls
    # back to ``cheapest``. Validated by the CLI / admin endpoint
    # before write — the DB treats it as opaque string.
    routing_strategy: Mapped[str | None] = mapped_column(String(64), nullable=True, default=None)

    # ---- Quality-aware routing data (Phase 24) ----
    # Per-team quality threshold (0..1) and per-model quality scores
    # populated by the eval harness. The router uses both fields when
    # ``routing_strategy="quality-aware-cheapest"``:
    #
    #   1. Eligible candidate models (capability filter passed)
    #   2. Drop any model whose stored score < quality_threshold
    #   3. Of what remains, pick the cheapest
    #
    # When ``quality_scores`` is NULL or empty the strategy falls back
    # to pure ``cheapest`` — no eval data, no quality filter to apply.
    # Threshold defaults to 0.7 (matching the eval harness's default
    # pass threshold) when NULL but the strategy is active.
    quality_threshold: Mapped[float | None] = mapped_column(nullable=True, default=None)
    # JSON shape:
    #   {
    #     "groq/llama-3.1-8b-instant": {
    #       "score": 1.0,
    #       "n_samples": 8,
    #       "source_eval_id": "2026-05-19T08:42:03Z",
    #       "ts": "2026-05-19T08:50:12Z"
    #     },
    #     ...
    #   }
    quality_scores: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True, default=None)

    # ---- Tool-use-aware routing data (Phase 46) ----
    # Mirror of ``quality_threshold`` + ``quality_scores`` but scored on
    # BFCL-style tool-use accuracy (Phase 45's per-model accuracy on the
    # curated golden set). The router consults these ONLY when:
    #   1. ``routing_strategy == "tool-use-aware-cheapest"``, AND
    #   2. The inbound request carries ``tools`` (no point filtering
    #      tool-less requests on tool-use accuracy).
    # When ``tool_use_scores`` is NULL the strategy falls back to plain
    # ``cheapest`` — no eval data, no filter to apply.
    # Threshold defaults to 0.9 (HIGHER than quality threshold because
    # tool-use sloppiness is operationally costly — wrong tool args
    # break agent loops; a few percentage points matter) when NULL but
    # the strategy is active.
    tool_use_threshold: Mapped[float | None] = mapped_column(nullable=True, default=None)
    # JSON shape (identical to ``quality_scores``):
    #   {
    #     "groq/llama-3.3-70b-versatile": {
    #       "score": 1.0,
    #       "n_samples": 12,
    #       "source_eval_id": "tool_use_basic-2026-05-21",
    #       "ts": "2026-05-21T17:02:00Z"
    #     },
    #     ...
    #   }
    tool_use_scores: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True, default=None
    )

    # ---- Prompt-cache-aware routing (Phase 47) ----
    # Composes Phases 34/35 (Anthropic + OpenAI prompt-cache extraction)
    # with Phase 46's routing scaffold. The new strategy
    # ``prompt-cache-aware-cheapest`` ranks eligible models by their
    # *expected* cost given a per-model rolling observation of the
    # team's prompt-cache hit rate. The observations themselves live
    # in Redis (see ``core.prompt_cache_observer.PromptCacheObserver``)
    # because they change continuously with traffic; the Team row
    # only carries the two thresholds operators tune.
    #
    # ``prompt_cache_min_samples`` — minimum sample count an fqmn must
    # have accumulated before its observed hit rate is trusted. Below
    # this, the router treats the fqmn as "unobserved" and falls back
    # to plain cost math for it. NULL → ``DEFAULT_PROMPT_CACHE_MIN_SAMPLES``
    # (20) when the strategy is active.
    prompt_cache_min_samples: Mapped[int | None] = mapped_column(
        Integer, nullable=True, default=None
    )
    # ``prompt_cache_min_hit_rate`` — floor on the observed hit rate;
    # models below this are scored on plain cost. NULL →
    # ``DEFAULT_PROMPT_CACHE_MIN_HIT_RATE`` (0.1). The 10% default is
    # the "this model's caching is meaningful enough to load-bear"
    # threshold; below 10% the savings adjustment is in the noise.
    prompt_cache_min_hit_rate: Mapped[float | None] = mapped_column(nullable=True, default=None)

    # ---- Reasoning-aware routing thresholds (Phase 57) ----
    # When ``routing_strategy == reasoning-aware-cheapest`` the scorer
    # multiplies each candidate's nominal output rate by
    # ``(1 + observed_reasoning_ratio)`` before picking the cheapest.
    # Observations themselves live in Redis (see
    # ``core.reasoning_observer.ReasoningObserver``) — the Team row
    # only carries the two thresholds operators tune.
    #
    # ``reasoning_aware_min_samples`` — minimum sample count an fqmn must
    # have accumulated before its observed reasoning ratio is trusted.
    # Below this, the router treats the fqmn as "unobserved" and scores
    # it at the nominal output rate. NULL →
    # ``DEFAULT_REASONING_MIN_SAMPLES`` (20).
    reasoning_aware_min_samples: Mapped[int | None] = mapped_column(
        Integer, nullable=True, default=None
    )
    # ``reasoning_aware_max_ratio`` — optional safety cap on observed
    # reasoning ratio. Models whose ratio EXCEEDS this value are
    # excluded from the candidate pool entirely (treated as too
    # reasoning-heavy regardless of base price). NULL → no exclusion
    # cap; the scorer ranks purely by effective cost.
    reasoning_aware_max_ratio: Mapped[float | None] = mapped_column(nullable=True, default=None)

    # ---- Tool-call result cache (Phase 49) ----
    # Caches tool execution results by (team_id, tool_name,
    # canonical_args_json). When a chat request arrives with trailing
    # ``assistant.tool_calls`` awaiting execution, the gateway checks
    # the cache for each pending call — on hit, a synthetic ``tool``
    # message is injected before the LLM call so the client doesn't
    # need to re-execute the tool. The cache is populated from past
    # chat requests where the client included the matching ``tool``
    # role result.
    #
    # OFF by default. Tool-result caching is only safe for
    # deterministic-in-args tools (``get_weather``,
    # ``lookup_user_by_id``, ``fetch_static_doc``). Tools with side
    # effects (``send_email``, ``delete_record``) or time-sensitive
    # results (``get_stock_price``) MUST stay uncached — operator
    # owns that policy decision per-team.
    tool_result_cache_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # TTL applied when cache records are written; NULL → scorer
    # default (1 hour). Tool results age out of correctness rapidly,
    # so the default is conservative; operators raise it for very
    # stable tools or lower it for borderline-cacheable ones.
    tool_result_cache_ttl_seconds: Mapped[int | None] = mapped_column(
        Integer, nullable=True, default=None
    )

    # ---- MCP client federation (Phase 54) ----
    # Bidirectional MCP: Pronaos has been an MCP server (Phases 48-51);
    # Phase 54 makes it an MCP CLIENT. A chat request can reference
    # external MCP servers via ``body.pronaos_mcp_servers``; the
    # gateway opens connections to them, federates their tools into
    # the chat completion, dispatches tool_calls back to the right
    # server, and loops until the LLM stops calling tools.
    #
    # The flag below gates whether a team's chat requests can
    # reference MCP servers at all. Off by default — stdio MCP
    # servers spawn subprocesses on the gateway host (arbitrary
    # command execution), so operators need to explicitly opt teams
    # in. A future phase can add a fine-grained per-command allowlist;
    # v1 is just enable/disable.
    mcp_client_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # ---- Async batches API (Phase 59) ----
    # OpenAI + Anthropic both ship async batch APIs at 50% of synchronous
    # pricing. Pronaos exposes them via ``POST /v1/batches`` with results
    # delivered over up to 24 hours. Off by default — batches accumulate
    # in the same monthly token budget as sync calls (half-priced per
    # call) but operators want explicit per-team opt-in because batch
    # workloads can be large + the FinOps shape differs.
    batches_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # ---- Request hedging (Phase 27) ----
    # Tail-latency reduction. When ``hedge_delay_ms`` is set, the failover
    # executor waits this long for the primary provider to return; if it
    # hasn't, an identical call is speculatively started against the next
    # provider in the chain (the "hedge"). Whichever finishes first wins;
    # the loser is cancelled. NULL or 0.0 = no hedging — the executor
    # walks the chain sequentially as before.
    #
    # Trade-off: hedge_delay_ms ~ primary p50 typically gives the best
    # p99-vs-cost ratio. Too low fires hedge on every call (~2x upstream
    # token spend); too high never fires (no tail-latency win).
    #
    # Reference: Dean & Barroso, "The Tail at Scale", CACM 2013.
    hedge_delay_ms: Mapped[float | None] = mapped_column(nullable=True, default=None)
    # Cap on how many hedge candidates fire per request, regardless of
    # chain length. NULL = 1 (race the primary against one alternative).
    # 0 = explicitly disabled even if ``hedge_delay_ms`` is set.
    hedge_max_count: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)

    # ---- A/B test config (Phase 29) ----
    # At most one ACTIVE A/B test per team; NULL = no test running and
    # routing behaves as usual. JSON shape::
    #
    #   {
    #     "id": "<uuid>",
    #     "name": "haiku-vs-sonnet",
    #     "started_at": "<iso8601 UTC>",
    #     "arm_a": {"model": "<provider/model>", "weight": <0..1>},
    #     "arm_b": {"model": "<provider/model>", "weight": <0..1>}
    #   }
    #
    # The gateway substitutes the request's model on a per-call
    # deterministic hash bucket (hash(team_id, ab_test_id, request_id))
    # so retries of the same logical request land in the same arm.
    # Per-call arm attribution is persisted to ``usage_records.ab_arm``
    # so the ``abtest report`` CLI can aggregate per-arm stats
    # (latency, cost, sample size) and compute Welch's t-test / chi²
    # for statistical significance on real production traffic.
    ab_test: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True, default=None)

    # ---- Agent-turn budgets (Phase 30) ----
    # Multi-call cost/token cap for tool-using agent executions. The
    # client supplies an ``X-Pronaos-Agent-Turn-ID`` header on every
    # call that belongs to the same logical agent turn; the gateway
    # accumulates running totals under that turn-id in Redis and
    # denies the call that would push the team over either budget.
    #
    # NULL on either budget column = unlimited (existing behaviour
    # for clients that don't use the header at all). Both gates can
    # deny independently; the stricter one wins. Counters persist
    # for ``agent_turn_ttl_seconds`` (default 3600) after the last
    # write, so a stuck or forgotten turn-id doesn't pin budget
    # forever.
    agent_turn_budget_tokens: Mapped[int | None] = mapped_column(
        Integer, nullable=True, default=None
    )
    agent_turn_budget_cost_hcents: Mapped[int | None] = mapped_column(
        Integer, nullable=True, default=None
    )
    agent_turn_ttl_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)

    # Phase 37 — per-tool budget caps. JSON shape:
    #   {"web_search":  {"limit_calls": 100, "current_calls": 23},
    #    "code_exec":   {"limit_calls": 50,  "current_calls": 5}}
    # NULL = no per-tool caps for this team (existing behaviour preserved).
    # ``current_calls`` resets together with the monthly_token_budget rollover
    # so the team's FinOps gates share one calendar-month boundary.
    # When ``current_calls >= limit_calls`` for a tool, the chat handler
    # strips that tool from the upstream request's ``tools`` array
    # before forwarding — the LLM never wastes reasoning on a
    # budget-exhausted tool.
    tool_budgets: Mapped[dict[str, dict[str, int]] | None] = mapped_column(
        JSON, nullable=True, default=None
    )

    # Phase 38 — reversible PII tokenization config.
    # ``pii_tokenization_enabled`` is the master switch; False keeps the
    # existing one-way REDACT behaviour. True lets the guardrail engine
    # honour ``TOKENIZE`` actions in ``guardrail_policy.rule_actions``
    # (e.g. ``{"pii.email": "tokenize"}``), at which point matched PII
    # gets replaced with deterministic per-tenant-salted tokens
    # (``[EMAIL_a3f7c2e1b890]``) before forwarding to the upstream, and
    # the gateway reverses the tokens in the response before returning
    # to the client. ``pii_token_ttl_seconds`` is the TTL on the Redis
    # mapping; NULL falls back to the gateway default (3600s).
    pii_tokenization_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    pii_token_ttl_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)

    # Phase 39 — structured output validation + auto-retry.
    # ``structured_output_max_retries`` caps how many times the chat
    # handler re-fires a completion when the response fails the
    # client-supplied JSON Schema. 0 disables retry (validation still
    # runs and the failure shows up in headers). 2 is the default —
    # covers the common case without unbounded amplification.
    # ``structured_output_provider_native`` controls whether the schema
    # is forwarded to the provider's native structured-output mechanism
    # (OpenAI ``response_format``); when False, prompt-injected schema
    # is used for every provider regardless of capability.
    structured_output_max_retries: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    structured_output_provider_native: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )

    # Phase 40 — quality regression detection + automated re-routing.
    # ``quality_sampling_rate`` is the per-team probability that any
    # given chat response gets sampled and scored by the LLM judge.
    # 0.0 (default) = sampling off; 0.01 = 1% sampling is the
    # operationally common value. ``quality_judge_model`` overrides
    # the gateway-wide judge default (typically a cheap fast model
    # like gpt-4o-mini). ``model_degradation_state`` is the per-model
    # degradation rollup the scorer reads when ``model="auto"``
    # routing fires — shape documented in the migration.
    quality_sampling_rate: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    quality_judge_model: Mapped[str | None] = mapped_column(
        String(255), nullable=True, default=None
    )
    model_degradation_state: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True, default=None
    )

    # Phase 41 — per-team base64 image-payload cap. Total bytes of all
    # base64 image parts in a single request must not exceed this value.
    # NULL = no cap (existing behaviour preserved for text-only teams).
    # HTTPS image URLs are NOT counted — the gateway never fetches them.
    max_image_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)

    tenant: Mapped[Tenant] = relationship("Tenant", back_populates="teams")
    api_keys: Mapped[list[ApiKey]] = relationship(
        "ApiKey", back_populates="team", cascade="all, delete-orphan"
    )

    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_teams_tenant_name"),)


# --------------------------------------------------------------------------- #
# ApiKey                                                                      #
# --------------------------------------------------------------------------- #


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    team_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("teams.id", ondelete="CASCADE"), nullable=False
    )
    # Short non-secret prefix for UX (so humans can identify the key without
    # exposing the secret). E.g. "fg_live_7a2c" → prefix="fg_live_7a2c".
    prefix: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    # argon2-cffi hash of the full key. Never log or expose this.
    key_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    # Space-separated scope tokens. Parsed at runtime; kept flat in DB for
    # easy grep-ability and trivial migration. Currently recognised:
    #   - "chat:write"   (required to hit /v1/chat/completions)
    #   - "admin"        (reserved for future tenant-admin endpoints)
    #   - "admin:usage"  (required to hit /v1/admin/usage — Phase 5.3)
    scopes: Mapped[str] = mapped_column(String(255), nullable=False, default="chat:write")
    # Human-friendly label set at issue time; distinguishes multiple keys
    # for the same team ("deploy-prod", "ci-runner").
    label: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    # Soft revocation: NULL = active, non-NULL = revoked at this time.
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
    # ---- Quota field (Phase 4) ----
    # Per-key requests-per-second cap; NULL means "unlimited." Stored as
    # whole RPS (Integer) because token-bucket math uses this value as the
    # bucket's burst and as the refill rate (1 r/s ↔ burst 1, refill 1/s).
    rps_limit: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)

    team: Mapped[Team] = relationship("Team", back_populates="api_keys")

    __table_args__ = (Index("ix_api_keys_team_id", "team_id"),)

    # ---- Convenience -----------------------------------------------------

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None

    def scope_list(self) -> list[str]:
        return [s for s in (self.scopes or "").split() if s]


# --------------------------------------------------------------------------- #
# UsageRecord                                                                 #
# --------------------------------------------------------------------------- #


class UsageRecord(Base):
    """One row per successful chat completion.

    Persists the data needed for FinOps (per-tenant chargeback, cost dashboards)
    and for operational queries ("which provider failed most this week").

    No foreign keys to ``api_keys`` / ``teams`` / ``tenants`` on purpose — when
    a tenant is deleted we DO want their usage history preserved (for compliance
    and finance). Soft FK via the id columns; queries join on demand.

    Indexes are scoped per common query shape:
    - ``(tenant_id, ts)`` for tenant-wide spend reports
    - ``(team_id, ts)`` for per-team chargeback (the hot path)
    """

    __tablename__ = "usage_records"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    # When the call completed (gateway clock; UTC). We use 'completed at' not
    # 'started at' because the upstream call duration is captured separately
    # in latency_ms.
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)

    # Soft references (no FK so usage survives tenant/team/key deletion).
    tenant_id: Mapped[str] = mapped_column(String(32), nullable=False)
    team_id: Mapped[str] = mapped_column(String(32), nullable=False)
    key_id: Mapped[str] = mapped_column(String(32), nullable=False)

    # What was called.
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(255), nullable=False)

    # Tokens and cost. ``cost_hcents`` is hundredths-of-a-cent for sub-cent
    # precision matching the providers/*.py pricing tables.
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_hcents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Tracing breadcrumb so usage rows can be joined with logs / future spans.
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True, default=None)

    # ``success`` for normal completions; future phases may add ``error`` or
    # ``cache_hit`` (Phase 8 semantic cache).
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="success")

    # ---- A/B test attribution (Phase 29) ----
    # When the request was routed by an active A/B test, this tags the
    # row with the arm letter ("a" or "b") so the ``abtest report`` CLI
    # can aggregate per-arm. NULL = call was not part of an A/B test
    # (the common case). The associated test id is NOT denormalised
    # here — operators read from ``teams.ab_test`` for context, then
    # filter usage rows by team_id + ab_arm + the test's ts window.
    ab_arm: Mapped[str | None] = mapped_column(String(4), nullable=True, default=None)

    # ---- Tool-call attribution (Phase 37) ----
    # Comma-separated list of tool names the LLM emitted in this call's
    # response (e.g. "web_search,fetch_url"). NULL when the call didn't
    # produce tool_calls — the common case for plain chat. Operator
    # query: "SELECT tool_names, COUNT(*), SUM(cost_hcents) FROM
    # usage_records WHERE team_id=? AND ts >= ? GROUP BY tool_names" —
    # which tools cost this team how much.
    tool_names: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)

    __table_args__ = (
        Index("ix_usage_records_team_ts", "team_id", "ts"),
        Index("ix_usage_records_tenant_ts", "tenant_id", "ts"),
    )


# --------------------------------------------------------------------------- #
# AuditRecord — Phase 10: hash-chained, tamper-evident audit trail            #
# --------------------------------------------------------------------------- #


class AuditRecord(Base):
    """One row per successful chat completion, **chained by hash**.

    Each record carries ``prev_hash`` (the previous row's ``this_hash``
    for the same tenant) and ``this_hash`` (a SHA-256 over the record's
    own fields plus ``prev_hash``). Any retroactive modification to a
    row breaks the chain at the next-newer row's hash, which an audit
    verifier can detect.

    Why per-tenant chains (rather than one global chain): per-tenant
    insertions don't contend on the global tail row's hash, so writes
    scale horizontally with tenant count. Verification queries also
    stay tenant-scoped, which is the normal compliance-audit shape.

    Why separate from ``usage_records`` (which already stores the same
    per-call metadata): different threat models and access patterns.
    ``usage_records`` is mutable-by-design — admins can correct
    miscoded provider/model fields after a backfill. ``audit_records``
    is meant to be append-only; the chain is the integrity proof.
    Operators who allow row-level writes to this table own the chain
    break themselves.

    Hash construction (kept narrow to make verification reproducible):
    ::

        this_hash = sha256(
            prev_hash | request_id | tenant_id | team_id | key_id |
            provider | model | ts_iso | request_hash | response_hash
        )

    All fields use ASCII string serialisation; the verifier reproduces
    exactly the same byte sequence. Don't mix in nullable fields without
    a stable canonical representation — that's the most common way to
    break chain reproducibility.
    """

    __tablename__ = "audit_records"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)

    # Soft references — same rationale as UsageRecord: audit history
    # survives tenant/team/key deletion.
    tenant_id: Mapped[str] = mapped_column(String(32), nullable=False)
    team_id: Mapped[str] = mapped_column(String(32), nullable=False)
    key_id: Mapped[str] = mapped_column(String(32), nullable=False)

    # What was called.
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(255), nullable=False)

    # Body hashes. Storing the raw bodies would defeat the "audit log
    # doesn't see PII" property and bloat the table; hashes alone are
    # enough to prove tamper-evidence and to confirm a specific
    # (request, response) pair if you've kept the bodies somewhere
    # else (logs, S3 export).
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    response_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    # Chain pointers. ``prev_hash`` is "" for the genesis record per
    # tenant — explicit empty string (not NULL) so the hash function
    # always sees a deterministic input.
    prev_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    this_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    # Tracing breadcrumb — same field as UsageRecord, lets you join
    # audit + usage on request_id for full-call reconstruction.
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True, default=None)

    # ---- Tool-call attribution (Phase 37) ----
    # Comma-separated list of tool names the LLM emitted in this call's
    # response. Stored OUTSIDE the hash chain (not in the canonical
    # this_hash input) to preserve hash backward-compatibility on
    # databases that pre-date Phase 37. Operators auditing tool usage
    # query this column directly; tamper-evidence still works because
    # the response_hash already covers the whole response body
    # including the tool_calls field.
    tool_names: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)

    __table_args__ = (
        # Per-tenant chain lookup: walk the chain in ts order, or find
        # the latest tail for a tenant before appending.
        Index("ix_audit_records_tenant_ts", "tenant_id", "ts"),
        # Direct hash lookup for the verifier: given a prev_hash, find
        # the row that produced it.
        Index("ix_audit_records_this_hash", "this_hash"),
    )


# --------------------------------------------------------------------------- #
# QualitySample — Phase 40: append-only log of judge-scored responses         #
# --------------------------------------------------------------------------- #


class QualitySample(Base):
    """One judge-scored sample of a production response.

    Append-only. Operators query this for trend analysis ("show me
    model X's quality over the last 24 hours") and the monitor reads
    it for the Welch's t-test against baseline. We deliberately do
    NOT store the original request/response bodies — the hash chain
    already covers those (Phase 10 ``audit_records``), and storing
    them again would defeat the goal of "audit log doesn't see PII"
    that drove the hash-only design.

    The ``score`` is the LLM judge's verdict in [0, 1]. We use Float
    because precision beyond 3 decimals is below the judge's noise
    floor — Decimal would be over-engineering."""

    __tablename__ = "quality_samples"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)

    # Soft references (no FK — samples survive tenant/team deletion
    # so historical-quality dashboards keep working after a team is
    # wound down).
    tenant_id: Mapped[str] = mapped_column(String(32), nullable=False)
    team_id: Mapped[str] = mapped_column(String(32), nullable=False)

    # The model being scored (e.g. ``groq/llama-3.1-8b-instant``).
    model: Mapped[str] = mapped_column(String(255), nullable=False)

    # Tracing breadcrumb — joins to usage_records / audit_records by
    # request_id when an operator needs the full call context. NULL
    # when sampling fires outside a normal request context (e.g. a
    # back-fill replay).
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True, default=None)

    # Score in [0, 1]. ``LLMJudgeScorer`` produces this on the same
    # scale as the eval framework — Claim #10 / #11 / #16 all share
    # the same normalisation so quality trends are comparable across
    # sample sources.
    score: Mapped[float] = mapped_column(Float, nullable=False)

    # Which judge model produced the score. Stored so operators can
    # audit "is this judge consistent" when quality numbers look
    # surprising. Required (NULL would lose attribution).
    judge_model: Mapped[str] = mapped_column(String(255), nullable=False)

    __table_args__ = (
        # The hot read path: monitor pulls the last N samples for
        # one (team, model) ordered by ts desc.
        Index("ix_quality_samples_team_model_ts", "team_id", "model", "ts"),
        # Per-tenant aggregation for FinOps / quality dashboards.
        Index("ix_quality_samples_tenant_ts", "tenant_id", "ts"),
    )


class Batch(Base):
    """Async batch job (Phase 59).

    Tracks one submission to the OpenAI or Anthropic batches API.
    Both providers ship batches at 50% of synchronous pricing with
    results delivered over up to 24 hours.

    State machine (Pronaos-normalized; both providers map onto it):

        validating  (submission accepted, provider parsing requests)
            ↓
        in_progress (provider running the batch)
            ↓
        finalizing  (provider compiling result file)
            ↓
        completed   (terminal — results available)

    or any of the failure terminals:
        failed | expired | cancelled

    The ``status`` column carries the Pronaos-normalized value;
    ``provider_batch_id`` is the upstream's opaque ID for polling.
    ``input_payload`` / ``output_payload`` carry the JSONL bodies for
    replay + audit (typical batch is a few KB to MB; stored inline
    for simplicity in v1, with future room to move to object storage).
    Per-request usage rows continue to land in ``usage_records``,
    each pointing back at this batch via the (yet-to-be-added)
    ``batch_id`` column on usage_records — for v1 the per-request
    rows aggregate up through ``request_id`` and the team-wide
    chargeback view splits batch from sync by joining on
    ``batches.created_at`` windows.
    """

    __tablename__ = "batches"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    team_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("teams.id", ondelete="CASCADE"), nullable=False
    )
    key_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("api_keys.id", ondelete="SET NULL"),
        nullable=True,
    )

    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    # Upstream's opaque batch id. NULL until submission completes
    # (during the brief moment when validation rejected the body
    # before reaching the provider).
    provider_batch_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="validating")
    endpoint: Mapped[str] = mapped_column(String(64), nullable=False)
    completion_window: Mapped[str] = mapped_column(String(16), nullable=False)

    request_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Aggregate token + cost totals. Populated when the batch completes
    # and the worker parses the result JSONL. Cost is at the 50% rate.
    prompt_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    cost_hcents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )
    in_progress_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # JSONL payloads. Empty string when not yet populated.
    input_payload: Mapped[str] = mapped_column(Text, nullable=False, default="")
    output_payload: Mapped[str] = mapped_column(Text, nullable=False, default="")

    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_batches_team_id", "team_id"),
        Index("ix_batches_status", "status"),
        Index("ix_batches_provider_batch_id", "provider_batch_id"),
    )
