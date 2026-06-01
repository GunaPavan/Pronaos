"""AuditVerifier — read side of the hash-chained audit trail.

Walks a tenant's chain in time order, recomputing every ``this_hash``
and comparing against what's stored. The first row whose computed hash
doesn't match flagged. Two possible causes:

1. **The row itself was tampered** (a field was modified after write).
   The verifier reports the row's id and which field group is implicated.
2. **The chain's prev_hash linkage is broken** (the predecessor row was
   modified, OR a row was deleted from the middle of the chain).

The verifier doesn't try to distinguish (1) from (2) — operationally,
either is a "chain broken at row X" event the compliance team
investigates. The point of the audit log isn't to fix tampering; it's
to MAKE tampering visible.

Cost
----
O(N) per tenant per verify. For a million-record chain that's a
single sequential scan plus N SHA-256 ops — typically sub-second.
Cheap enough to run nightly in CI.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pronaos.audit.logger import canonical_ts, hash_inputs
from pronaos.db.models import AuditRecord

# --------------------------------------------------------------------------- #
# Result types                                                                #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ChainBreak:
    """One break point detected by the verifier."""

    record_id: str
    ts_iso: str
    reason: str  # "hash_mismatch" | "prev_hash_mismatch" | "missing_predecessor"
    expected_hash: str
    actual_hash: str


@dataclass(slots=True)
class VerifyResult:
    """Outcome of one verify pass for one tenant."""

    tenant_id: str
    total_records: int
    verified_records: int
    breaks: list[ChainBreak] = field(default_factory=list)

    @property
    def is_intact(self) -> bool:
        return not self.breaks and self.verified_records == self.total_records


# --------------------------------------------------------------------------- #
# Verifier                                                                    #
# --------------------------------------------------------------------------- #


class AuditVerifier:
    """Walks a tenant's audit chain and reports tamper points."""

    async def verify(self, session: AsyncSession, tenant_id: str) -> VerifyResult:
        """Walk the entire chain for ``tenant_id`` and produce a verdict."""
        result = VerifyResult(tenant_id=tenant_id, total_records=0, verified_records=0)

        stmt = (
            select(AuditRecord)
            .where(AuditRecord.tenant_id == tenant_id)
            .order_by(AuditRecord.ts.asc(), AuditRecord.id.asc())
        )
        rows = (await session.execute(stmt)).scalars().all()
        result.total_records = len(rows)

        expected_prev = ""  # the genesis link
        for row in rows:
            # 1. Check the row's prev_hash matches what we expected from
            #    the previous row's this_hash. A mismatch here means
            #    EITHER the previous row was tampered OR a row was
            #    deleted from the middle.
            if row.prev_hash != expected_prev:
                result.breaks.append(
                    ChainBreak(
                        record_id=row.id,
                        ts_iso=row.ts.isoformat(),
                        reason="prev_hash_mismatch",
                        expected_hash=expected_prev,
                        actual_hash=row.prev_hash,
                    )
                )
                # Don't bail — keep verifying so a single break in the
                # middle doesn't hide later breaks. Use this row's
                # stored this_hash as the new expected_prev so we
                # continue the walk.
                expected_prev = row.this_hash
                continue

            # 2. Recompute this_hash and compare against what's stored.
            recomputed = hash_inputs(
                prev_hash=row.prev_hash,
                request_id=row.request_id or "",
                tenant_id=row.tenant_id,
                team_id=row.team_id,
                key_id=row.key_id,
                provider=row.provider,
                model=row.model,
                ts_iso=canonical_ts(row.ts),
                request_hash=row.request_hash,
                response_hash=row.response_hash,
            )
            if recomputed != row.this_hash:
                result.breaks.append(
                    ChainBreak(
                        record_id=row.id,
                        ts_iso=row.ts.isoformat(),
                        reason="hash_mismatch",
                        expected_hash=recomputed,
                        actual_hash=row.this_hash,
                    )
                )
            else:
                result.verified_records += 1

            expected_prev = row.this_hash

        return result
