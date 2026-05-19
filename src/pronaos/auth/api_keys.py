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
    # One of ``cheapest`` | ``fastest`` | ``balanced``. NULL = no
    # preference; the gateway falls back to ``cheapest``.
    routing_strategy: str | None = None

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
    )


# A known-invalid argon2 hash we run through verify() when lookup failed,
# keeping the code path timing comparable to the happy path.
_DUMMY_HASH = _hasher.hash("pronaos-dummy")


def _dummy_verify() -> None:
    with contextlib.suppress(VerifyMismatchError):
        _hasher.verify(_DUMMY_HASH, "pronaos-dummy-wrong")
