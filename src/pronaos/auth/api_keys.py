"""API key issuance + verification.

Design
------
- Format: ``pn_<env>_<12-char-prefix>_<random-body>``
  - ``pn_`` is a Pronaos marker so keys are grep-able if they leak.
  - ``<env>`` is ``live`` or ``test`` so accidents across envs are obvious.
  - ``<prefix>`` is stored in the DB for identification (not secret).
  - ``<body>`` is URL-safe random, ~32 bytes of entropy.
- Storage: argon2 hash of the full key (prefix + body), constant-time verified.
- Lookup: we query by ``prefix`` (indexed) to narrow to a single candidate,
  then verify the hash. This keeps lookups O(1) without exposing the secret
  to any index.

Why argon2 and not bcrypt?
    argon2 is the winner of the 2015 PHC; it's memory-hard (resistant to
    GPU/ASIC brute force) and is the modern standard recommended by OWASP.
    bcrypt is fine too but argon2 is strictly stronger and is what every new
    auth system reaches for today.
"""

from __future__ import annotations

import contextlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pronaos.db.models import ApiKey, Team, Tenant

KEY_PREFIX: Final = "pn"
_PREFIX_BYTES: Final = 6  # 12 hex chars
_BODY_BYTES: Final = 24  # ~32 URL-safe chars
KEY_TOTAL_LEN: Final = len(KEY_PREFIX) + 1 + 4 + 1 + _PREFIX_BYTES * 2 + 1 + _BODY_BYTES


# Single shared hasher — argon2 params are deliberately on the expensive side
# of "interactive login" so password-guessing is infeasible even if the DB is
# exfiltrated. We can tune down for perf later if it becomes a bottleneck.
_hasher = PasswordHasher(time_cost=2, memory_cost=64_000, parallelism=1)


@dataclass(frozen=True, slots=True)
class Principal:
    """Who made this request, as resolved from a valid API key."""

    tenant_id: str
    tenant_name: str
    team_id: str
    team_name: str
    key_id: str
    key_prefix: str
    scopes: frozenset[str]
    # Phase 4 quota fields surfaced at auth time so the rate-limit gate
    # below doesn't need a second DB hit. ``rps_limit`` is per-key;
    # ``monthly_token_budget`` is per-team. The QuotaTracker is
    # authoritative for the running counter regardless.
    rps_limit: int | None = None
    monthly_token_budget: int | None = None
    # Phase 8.2: per-team guardrail policy override. None = use engine
    # defaults. JSON shape (raw, validated downstream by the resolver):
    #   {"disabled_rules": ["pii.ipv4"], "rule_actions": {"injection": "block"}}
    guardrail_policy: dict[str, Any] | None = None
    # Phase 17: per-team model allowlist. None = unrestricted; list of
    # fnmatch-style patterns matched against the request's ``model``.
    # See ``core/model_access.py`` for the matching helper.
    allowed_models: list[str] | None = None
    # Phase 19: per-tenant webhook config. NULL on either field means
    # webhooks are disabled for this tenant. Pulled from Tenant rather
    # than Team because operational events (quota exhaustion, breaker
    # trips, chain breaks) are typically routed to ONE incident
    # channel per organisation — not per-team. Surfacing on Principal
    # saves a DB hit in the hot publish-points.
    webhook_url: str | None = None
    webhook_secret: str | None = None
    # Phase 21: per-team routing strategy for ``model="auto"`` requests.
    # One of ``cheapest`` | ``fastest`` | ``balanced`` |
    # ``quality-aware-cheapest`` (Phase 24). NULL = no preference;
    # the gateway falls back to ``cheapest``.
    routing_strategy: str | None = None
    # Phase 24: quality-aware routing data.
    # ``quality_threshold`` is the floor in 0..1 for the
    # ``quality-aware-cheapest`` strategy; NULL falls back to 0.7
    # when the strategy is active. ``quality_scores`` maps the team's
    # fqmn → {"score": float, "n_samples": int, ...} as written by
    # ``pronaos-cli eval store-scores``.
    quality_threshold: float | None = None
    quality_scores: dict[str, Any] | None = None
    # Phase 46: tool-use-aware routing data. Same JSON shape as
    # ``quality_scores`` but scored on BFCL-style tool-call accuracy
    # (Phase 45). The router consults these only when the strategy is
    # ``tool-use-aware-cheapest`` AND the inbound request carries tools.
    tool_use_threshold: float | None = None
    tool_use_scores: dict[str, Any] | None = None
    # Phase 47: prompt-cache-aware routing thresholds. The runtime
    # observations themselves live in Redis (see
    # ``core.prompt_cache_observer.PromptCacheObserver``), NOT on the
    # team row, because they change continuously with traffic. The
    # router only needs the two thresholds at request time — sample
    # count gate + hit-rate gate. NULL on either means "use the
    # scorer's default" (20 samples / 0.10 hit rate). Same opt-in
    # semantics as Phases 24 + 46.
    prompt_cache_min_samples: int | None = None
    prompt_cache_min_hit_rate: float | None = None
    # Phase 57: reasoning-aware routing thresholds. Observations live
    # in Redis (ReasoningObserver), the Principal only carries the
    # two gates. NULL on either means "use the scorer's default"
    # (20 samples / no max-ratio cap).
    reasoning_aware_min_samples: int | None = None
    reasoning_aware_max_ratio: float | None = None
    # Phase 49: tool-call result caching. When enabled, the chat
    # handler memoizes (tool_name, args) → result extracted from
    # past ``tool`` role messages and injects cached results into
    # subsequent requests whose trailing assistant.tool_calls are
    # awaiting execution. Disabled by default — caching only makes
    # sense for deterministic-in-args tools; operator owns the
    # per-team opt-in. TTL NULL → ``DEFAULT_TTL_SECONDS`` (1 hour).
    tool_result_cache_enabled: bool = False
    tool_result_cache_ttl_seconds: int | None = None
    # Phase 54: MCP client federation per-team enable flag. When True,
    # the chat handler honours ``body.pronaos_mcp_servers`` and federates
    # external MCP servers' tools into the chat completion. When False,
    # the field is rejected with 422 ``mcp_client_disabled``. Off by
    # default because stdio MCP servers spawn subprocesses (security-
    # sensitive — arbitrary command execution).
    mcp_client_enabled: bool = False
    # Phase 59: per-team gate on the async batches API. Off by default
    # because batch workloads can be large + operators want explicit
    # opt-in. The endpoint returns 422 ``batches_disabled`` for teams
    # without this flag.
    batches_enabled: bool = False
    # Phase 26: auth method used to resolve this Principal. ``"api_key"``
    # = the existing argon2-hashed API key path; ``"oidc"`` = an OIDC
    # JWT verified at request time. Surfaces in audit logs so a tenant
    # operator can see "an admin logged in via SSO at 14:32 UTC."
    auth_method: str = "api_key"
    # Phase 27: request-hedging policy. ``hedge_delay_ms`` is the wall
    # time the failover executor waits for the primary provider before
    # speculatively starting an identical call against the next chain
    # provider. NULL or 0.0 = no hedging (sequential failover, the
    # default). ``hedge_max_count`` caps how many hedges may fire per
    # request, regardless of chain length; NULL is treated as 1.
    hedge_delay_ms: float | None = None
    hedge_max_count: int | None = None
    # Phase 29: active A/B test config. Raw JSON dict matching the
    # shape stored in ``teams.ab_test``; parsed to ``ABTest`` by the
    # chat handler when an arm needs to be resolved. None = no test
    # running, routing behaves as usual.
    ab_test: dict[str, Any] | None = None
    # Phase 30: per-execution budget for tool-using agent loops.
    # Client signals participation via the ``X-Pronaos-Agent-Turn-ID``
    # header; gateway accumulates running totals per turn-id and
    # denies the call that would push the team over either budget.
    # NULL on either budget column = unlimited; matches the existing
    # behaviour for teams that don't use the feature.
    agent_turn_budget_tokens: int | None = None
    agent_turn_budget_cost_hcents: int | None = None
    agent_turn_ttl_seconds: int | None = None

    # ---- Per-tool budgets (Phase 37) ----
    # JSON shape on the team row:
    #   {"web_search":  {"limit_calls": 100, "current_calls": 23}, ...}
    # The chat handler reads ``current_calls`` vs ``limit_calls`` and
    # strips over-budget tools from the upstream request body before
    # forwarding. ``current_calls`` is incremented in ``quota.record_call``
    # for each tool name the LLM emitted in the response. Resets together
    # with the team's monthly token-budget rollover. NULL = no per-tool
    # caps (existing behaviour preserved).
    tool_budgets: dict[str, dict[str, int]] | None = None

    # ---- Reversible PII tokenization (Phase 38) ----
    # Master switch + TTL surfaced on Principal so the chat handler
    # doesn't need a second DB hit per request. When the switch is
    # False, the engine ignores any ``TOKENIZE`` actions in policy
    # and falls back to ``REDACT`` (preserves existing one-way behaviour
    # for teams that haven't opted in). When True, the chat handler
    # writes the verdict's tokenization mappings into Redis using the
    # configured TTL (NULL => 3600s default).
    pii_tokenization_enabled: bool = False
    pii_token_ttl_seconds: int | None = None

    # ---- Structured output (Phase 39) ----
    # ``structured_output_max_retries`` caps the gateway-side retry
    # loop on JSON Schema validation failures. 0 disables retry
    # (validation still runs; failures show up in the response
    # headers). Default 2 covers the common case.
    # ``structured_output_provider_native`` controls whether the schema
    # is forwarded to provider-native structured-output mechanisms
    # (OpenAI ``response_format``); False forces prompt-injected schema
    # on every provider, useful when a provider's native impl has known
    # bugs.
    structured_output_max_retries: int = 2
    structured_output_provider_native: bool = True

    # ---- Quality regression monitoring (Phase 40) ----
    # ``quality_sampling_rate`` is the per-team probability of
    # judge-scoring any given response. 0.0 = sampling off.
    # ``quality_judge_model`` overrides the gateway-wide judge default
    # (typically a cheap fast model like gpt-4o-mini).
    # ``model_degradation_state`` is read by the routing scorer to
    # exclude degraded models from the auto-routing candidate pool.
    quality_sampling_rate: float = 0.0
    quality_judge_model: str | None = None
    model_degradation_state: dict[str, Any] | None = None

    # ---- Multi-modal image cap (Phase 41) ----
    # NULL = no cap; integer = bytes of base64 image payload per
    # request. The chat handler rejects with 422 when the sum of all
    # base64 image content exceeds this. URL-based images aren't
    # measured — the gateway never fetches them.
    max_image_bytes: int | None = None

    def has_scope(self, required: str) -> bool:
        return required in self.scopes


def generate_api_key(env: str = "live") -> tuple[str, str]:
    """Produce ``(full_key, prefix)``.

    Only the full key is returned to the caller — it must be shown to the
    user once and never stored. The prefix is what goes into the DB for
    lookup/identification.
    """
    prefix = secrets.token_hex(_PREFIX_BYTES)
    body = secrets.token_urlsafe(_BODY_BYTES)
    full = f"{KEY_PREFIX}_{env}_{prefix}_{body}"
    return full, prefix


def hash_key(full_key: str) -> str:
    """Return an argon2 hash of the full key, suitable for DB storage."""
    return _hasher.hash(full_key)


def _parse_key(raw: str) -> tuple[str, str] | None:
    """Extract ``(prefix, full_key)`` from a bearer string; ``None`` if malformed.

    Shape: ``pn_<env>_<prefix>_<body>`` (4 underscore-separated parts).
    """
    parts = raw.split("_", 3)
    if len(parts) != 4 or parts[0] != KEY_PREFIX:
        return None
    _, _, prefix, _body = parts
    if len(prefix) != _PREFIX_BYTES * 2:
        return None
    return prefix, raw


async def verify_key(session: AsyncSession, raw_key: str) -> Principal | None:
    """Validate a bearer token against the DB. Returns a ``Principal`` if valid.

    Returns ``None`` on any failure (malformed, unknown prefix, hash
    mismatch, revoked). We deliberately do **not** distinguish reasons to
    callers — HTTP-level auth should always surface as a generic 401.
    """
    parsed = _parse_key(raw_key)
    if parsed is None:
        return None
    prefix, full = parsed

    stmt = (
        select(ApiKey, Team, Tenant)
        .join(Team, ApiKey.team_id == Team.id)
        .join(Tenant, Team.tenant_id == Tenant.id)
        .where(ApiKey.prefix == prefix)
    )
    result = await session.execute(stmt)
    row = result.first()
    if row is None:
        # No matching prefix. Still do a dummy hash verification so timing
        # doesn't leak whether the prefix existed.
        _dummy_verify()
        return None

    api_key, team, tenant = row
    if api_key.revoked_at is not None:
        _dummy_verify()
        return None

    # Constant-time prefix check as a belt-and-braces defence in depth.
    if not hmac.compare_digest(api_key.prefix, prefix):
        _dummy_verify()
        return None

    try:
        _hasher.verify(api_key.key_hash, full)
    except VerifyMismatchError:
        return None

    # Best-effort last-used timestamp update. Non-fatal if commit fails.
    api_key.last_used_at = datetime.now(tz=UTC)

    return Principal(
        tenant_id=tenant.id,
        tenant_name=tenant.name,
        team_id=team.id,
        team_name=team.name,
        key_id=api_key.id,
        key_prefix=api_key.prefix,
        scopes=frozenset(api_key.scope_list()),
        rps_limit=api_key.rps_limit,
        monthly_token_budget=team.monthly_token_budget,
        guardrail_policy=team.guardrail_policy,
        allowed_models=team.allowed_models,
        webhook_url=tenant.webhook_url,
        webhook_secret=tenant.webhook_secret,
        routing_strategy=team.routing_strategy,
        quality_threshold=team.quality_threshold,
        quality_scores=team.quality_scores,
        tool_use_threshold=team.tool_use_threshold,
        tool_use_scores=team.tool_use_scores,
        prompt_cache_min_samples=team.prompt_cache_min_samples,
        prompt_cache_min_hit_rate=team.prompt_cache_min_hit_rate,
        reasoning_aware_min_samples=team.reasoning_aware_min_samples,
        reasoning_aware_max_ratio=team.reasoning_aware_max_ratio,
        tool_result_cache_enabled=team.tool_result_cache_enabled,
        tool_result_cache_ttl_seconds=team.tool_result_cache_ttl_seconds,
        mcp_client_enabled=team.mcp_client_enabled,
        batches_enabled=team.batches_enabled,
        hedge_delay_ms=team.hedge_delay_ms,
        hedge_max_count=team.hedge_max_count,
        ab_test=team.ab_test,
        agent_turn_budget_tokens=team.agent_turn_budget_tokens,
        agent_turn_budget_cost_hcents=team.agent_turn_budget_cost_hcents,
        agent_turn_ttl_seconds=team.agent_turn_ttl_seconds,
        tool_budgets=team.tool_budgets,
        pii_tokenization_enabled=team.pii_tokenization_enabled,
        pii_token_ttl_seconds=team.pii_token_ttl_seconds,
        structured_output_max_retries=team.structured_output_max_retries,
        structured_output_provider_native=team.structured_output_provider_native,
        quality_sampling_rate=team.quality_sampling_rate,
        quality_judge_model=team.quality_judge_model,
        model_degradation_state=team.model_degradation_state,
        max_image_bytes=team.max_image_bytes,
    )


# A known-invalid argon2 hash we run through verify() when lookup failed,
# keeping the code path timing comparable to the happy path.
_DUMMY_HASH = _hasher.hash("pronaos-dummy")


def _dummy_verify() -> None:
    with contextlib.suppress(VerifyMismatchError):
        _hasher.verify(_DUMMY_HASH, "pronaos-dummy-wrong")


async def resolve_oidc_subject(session: AsyncSession, subject: str) -> Principal | None:
    """Find the tenant whose ``oidc_subject`` matches the OIDC JWT's
    ``sub`` claim, and build a tenant-admin Principal for it.

    Returns ``None`` when no tenant claims this subject — the caller
    surfaces a 401. Doesn't carry team-level fields (rps_limit, budgets,
    guardrail_policy, etc.) because the OIDC path is for tenant-admin
    operations only; the chat endpoint requires the ``chat:write``
    scope, which OIDC principals deliberately don't get.

    Phase 26.
    """
    stmt = select(Tenant).where(Tenant.oidc_subject == subject)
    result = await session.execute(stmt)
    tenant = result.scalar_one_or_none()
    if tenant is None:
        return None
    return Principal(
        tenant_id=tenant.id,
        tenant_name=tenant.name,
        # OIDC principals authenticate to a *tenant*, not a team. The
        # admin endpoints look up team_id from the URL path; chat
        # endpoints require ``chat:write`` which the OIDC path
        # deliberately doesn't grant.
        team_id="",
        team_name="",
        key_id=f"oidc:{subject}",
        key_prefix="oidc",
        # Tenant-admin scope only. No ``chat:write``: human admins
        # shouldn't be billable LLM-call sources by accident.
        scopes=frozenset({"admin:usage"}),
        webhook_url=tenant.webhook_url,
        webhook_secret=tenant.webhook_secret,
        auth_method="oidc",
    )
