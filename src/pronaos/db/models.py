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

from sqlalchemy import DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Shared declarative base for all ORM models."""


def _new_id() -> str:
    return uuid.uuid4().hex


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


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
    # easy grep-ability and trivial migration. Phase 3 recognises:
    #   - "chat:write" (required to hit /v1/chat/completions)
    #   - "admin"      (required to hit /v1/admin/*)
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

    team: Mapped[Team] = relationship("Team", back_populates="api_keys")

    __table_args__ = (Index("ix_api_keys_team_id", "team_id"),)

    # ---- Convenience -----------------------------------------------------

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None

    def scope_list(self) -> list[str]:
        return [s for s in (self.scopes or "").split() if s]
