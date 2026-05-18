"""AuditLogger — write side of the hash-chained audit trail.

Hash construction is intentionally narrow and explicit. Every field
that goes into the hash is named here; nothing is implicit. The
verifier in ``audit.verifier`` calls the same ``hash_inputs()``
function — they MUST agree byte-for-byte or every chain breaks.

Per-tenant chains
-----------------
``append()`` reads the most recent row for the tenant (highest ``ts``,
break ties with id) to get ``prev_hash``. The new row's ``this_hash``
is ``sha256(prev_hash || canonical_inputs)``.

Why per-tenant: a global chain would force every write to contend on
the global tail. Per-tenant chains scale independently, and the
verification audit shape (compliance teams query their own tenant's
chain) maps naturally to it.

Race condition
--------------
Two concurrent inserts for the same tenant can read the same
``prev_hash`` and produce two rows pointing at the same predecessor —
a fork. We do NOT serialise per-tenant writes today; the cost would
be a table-level lock. The verifier handles forks by walking BOTH
branches and reporting if either tampered. For most compliance
use-cases this is acceptable; teams that need linearised chains can
add per-tenant advisory locks in a follow-up.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pronaos.db.models import AuditRecord
from pronaos.logging import get_logger

log = get_logger(__name__)


# --------------------------------------------------------------------------- #
# Hash helpers                                                                #
# --------------------------------------------------------------------------- #


def hash_body(payload: dict[str, Any] | list[Any] | str) -> str:
    """Stable SHA-256 over a request or response body.

    JSON serialisation uses ``sort_keys=True`` + tight separators so
    cosmetic differences (key order, whitespace) don't produce
    different hashes for semantically identical bodies. The verifier
    uses the same routine."""
    if isinstance(payload, str):
        text = payload
    else:
        text = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_ts(ts: datetime) -> str:
    """Stable string form of a timestamp for hashing.

    SQLite drops tzinfo on read-back: a write-side ``ts`` carries
    ``tzinfo=UTC`` (set by the ORM default ``datetime.now(tz=UTC)``)
    but the verifier reads it back as naive. ``.isoformat()`` produces
    different strings in those two cases, which breaks every hash.

    The fix: normalise to **naive UTC** before isoformat on BOTH sides.
    Naive datetimes from SQLite are implicitly UTC because we always
    wrote them in UTC; this just makes the convention explicit and
    reproducible at hash time."""
    if ts.tzinfo is not None:
        ts = ts.astimezone(UTC).replace(tzinfo=None)
    return ts.isoformat()


def hash_inputs(
    *,
    prev_hash: str,
    request_id: str,
    tenant_id: str,
    team_id: str,
    key_id: str,
    provider: str,
    model: str,
    ts_iso: str,
    request_hash: str,
    response_hash: str,
) -> str:
    """Canonical hash function — used by writer AND verifier.

    Fields are concatenated with a delimiter that can't appear in any
    field (``\\x1f`` is the ASCII Unit Separator — Python's repr safe
    but byte-distinct from anything a model or path can produce). The
    order is fixed by this function's keyword argument order; changing
    that order would invalidate every existing chain.
    """
    SEP = "\x1f"
    blob = SEP.join(
        [
            prev_hash,
            request_id or "",
            tenant_id,
            team_id,
            key_id,
            provider,
            model,
            ts_iso,
            request_hash,
            response_hash,
        ]
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# AuditLogger                                                                  #
# --------------------------------------------------------------------------- #


class AuditLogger:
    """Append one chain-linked record per successful chat call."""

    async def append(
        self,
        session: AsyncSession,
        *,
        tenant_id: str,
        team_id: str,
        key_id: str,
        provider: str,
        model: str,
        request_body: dict[str, Any] | list[Any],
        response_body: dict[str, Any] | list[Any],
        request_id: str | None = None,
    ) -> AuditRecord | None:
        """Write one audit record. Fail-open: any error logs and returns ``None``.

        Returns the persisted ``AuditRecord`` on success so callers can
        inspect ``this_hash`` (useful for response headers / span
        attributes / debugging). ``None`` on failure — callers should
        treat that as "audit gap" not "request failed.\""""
        try:
            prev = await self._latest_for_tenant(session, tenant_id)
            prev_hash = prev.this_hash if prev is not None else ""

            request_hash = hash_body(request_body)
            response_hash = hash_body(response_body)

            # Use a fresh AuditRecord instance — it generates its own
            # id and ts via column defaults. We compute this_hash
            # against those, then write.
            record = AuditRecord(
                tenant_id=tenant_id,
                team_id=team_id,
                key_id=key_id,
                provider=provider,
                model=model,
                request_hash=request_hash,
                response_hash=response_hash,
                prev_hash=prev_hash,
                this_hash="",  # placeholder, set below
                request_id=request_id,
            )

            # Trigger column defaults so id + ts are populated before
            # we hash. ``session.add`` alone doesn't run them; flush
            # does.
            session.add(record)
            await session.flush()

            ts_iso = canonical_ts(record.ts)
            record.this_hash = hash_inputs(
                prev_hash=prev_hash,
                request_id=request_id or "",
                tenant_id=tenant_id,
                team_id=team_id,
                key_id=key_id,
                provider=provider,
                model=model,
                ts_iso=ts_iso,
                request_hash=request_hash,
                response_hash=response_hash,
            )
            # No need to flush again; the wrapping session.commit() will
            # send the UPDATE for this_hash alongside the INSERT.
            return record
        except Exception as e:
            log.warning(
                "audit.append_failed",
                tenant_id=tenant_id,
                error=str(e),
            )
            return None

    @staticmethod
    async def _latest_for_tenant(
        session: AsyncSession, tenant_id: str
    ) -> AuditRecord | None:
        """Find the tail of the tenant's chain. Order by (ts DESC, id DESC)
        so concurrent inserts at identical timestamps still produce a
        deterministic order."""
        stmt = (
            select(AuditRecord)
            .where(AuditRecord.tenant_id == tenant_id)
            .order_by(AuditRecord.ts.desc(), AuditRecord.id.desc())
            .limit(1)
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()
