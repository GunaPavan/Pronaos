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
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
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
    current_period_cost_hcents: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )
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
    allowed_models: Mapped[list[str] | None] = mapped_column(
        JSON, nullable=True, default=None
    )

    # ---- Routing strategy (Phase 21) ----
    # Selects how ``model="auto"`` requests are resolved to a concrete
    # provider/model. One of ``cheapest`` | ``fastest`` | ``balanced``
    # (the wire format of ``RoutingStrategy``). NULL = no preference;
    # the gateway falls back to ``cheapest``. Validated by the CLI /
    # admin endpoint before write — the DB treats it as opaque string.
    routing_strategy: Mapped[str | None] = mapped_column(
        String(32), nullable=True, default=None
    )

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
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

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
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

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
    request_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, default=None
    )

    __table_args__ = (
        # Per-tenant chain lookup: walk the chain in ts order, or find
        # the latest tail for a tenant before appending.
        Index("ix_audit_records_tenant_ts", "tenant_id", "ts"),
        # Direct hash lookup for the verifier: given a prev_hash, find
        # the row that produced it.
        Index("ix_audit_records_this_hash", "this_hash"),
    )
