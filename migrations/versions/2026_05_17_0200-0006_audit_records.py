"""audit_records — hash-chained tamper-evident audit trail (Phase 10).

Each row references the previous row's hash for the same tenant, forming
a per-tenant chain. Any retroactive mutation breaks the chain at the
next-newer row, which the ``audit verify`` CLI detects in O(N) per
tenant.

Schema notes
------------
- ``request_hash`` and ``response_hash`` are SHA-256 digests
  (64 hex chars), stored as String(64). The raw bodies are NOT stored
  — that would re-introduce the PII problem the gateway's guardrails
  exist to solve.
- ``prev_hash`` defaults to empty string (NOT NULL) so the hash inputs
  are always deterministic. The genesis record for each tenant has
  ``prev_hash == ""``.
- Two indexes:
    (tenant_id, ts) — chain walk + tail lookup
    this_hash       — verifier reverse-lookup

Revision ID: 0006
Revises: 0005
Create Date: 2026-05-17
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "audit_records",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column(
            "ts",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.Column("tenant_id", sa.String(length=32), nullable=False),
        sa.Column("team_id", sa.String(length=32), nullable=False),
        sa.Column("key_id", sa.String(length=32), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=255), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("response_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "prev_hash", sa.String(length=64), nullable=False, server_default=""
        ),
        sa.Column("this_hash", sa.String(length=64), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_audit_records_tenant_ts",
        "audit_records",
        ["tenant_id", "ts"],
    )
    op.create_index(
        "ix_audit_records_this_hash",
        "audit_records",
        ["this_hash"],
    )


def downgrade() -> None:
    op.drop_index("ix_audit_records_this_hash", table_name="audit_records")
    op.drop_index("ix_audit_records_tenant_ts", table_name="audit_records")
    op.drop_table("audit_records")
