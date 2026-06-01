"""Async batches API — `batches` table + Team.batches_enabled

Phase 59 adds the Anthropic + OpenAI batches API surface. Both
providers ship async batch endpoints at 50% of synchronous pricing,
with results delivered over up to 24 hours. Pronaos exposes a single
``POST /v1/batches`` that routes by model prefix and tracks each
batch's lifecycle in this new table.

Storage model
-------------
One row per submitted batch. The row carries:

- ``provider`` + ``provider_batch_id``: which upstream owns it
- ``status``: Pronaos-normalized state (validating | in_progress |
  finalizing | completed | failed | expired | cancelled)
- ``endpoint``: which API the batch targets (``/v1/chat/completions``
  or ``/v1/embeddings`` — v1 ships chat-only but the column is in
  place for the followup phase)
- counts: ``request_count``, ``completed_count``, ``failed_count``
- aggregate usage: ``prompt_tokens``, ``completion_tokens``,
  ``cost_hcents`` (the half-priced total)
- timestamps: ``created_at``, ``in_progress_at``, ``completed_at``
- ``input_payload``: the original inline requests JSONL (for replay
  + audit). Stored as Text — typical batch is a few KB to MB.
- ``output_payload``: the result JSONL pulled from the provider on
  completion. Same storage shape.

Per-request usage rows continue to land in ``usage_records`` keyed
to the team — the batch_id is stored in a separate column so
chargeback queries can split sync vs batch spend.

Per-team gate
-------------
``teams.batches_enabled`` — BOOLEAN, default FALSE. The endpoint
checks this gate; an unenabled team gets HTTP 422
``batches_disabled``. Operators turn it on per-team because batch
quota usage is non-trivial and the operator wants explicit opt-in.

Revision ID: 0025
Revises: 0024
Create Date: 2026-05-30
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---- Team.batches_enabled gate ------------------------------------
    with op.batch_alter_table("teams") as batch:
        batch.add_column(
            sa.Column(
                "batches_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            )
        )

    # ---- batches table -------------------------------------------------
    op.create_table(
        "batches",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.String(length=64),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "team_id",
            sa.String(length=64),
            sa.ForeignKey("teams.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "key_id",
            sa.String(length=64),
            sa.ForeignKey("api_keys.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("provider_batch_id", sa.String(length=128), nullable=True),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'validating'"),
        ),
        sa.Column("endpoint", sa.String(length=64), nullable=False),
        sa.Column("completion_window", sa.String(length=16), nullable=False),
        sa.Column("request_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("prompt_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column(
            "completion_tokens", sa.BigInteger(), nullable=False, server_default="0"
        ),
        sa.Column("cost_hcents", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("in_progress_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "input_payload",
            sa.Text(),
            nullable=False,
            server_default=sa.text("''"),
        ),
        sa.Column(
            "output_payload",
            sa.Text(),
            nullable=False,
            server_default=sa.text("''"),
        ),
        sa.Column("error_message", sa.Text(), nullable=True),
    )
    op.create_index("ix_batches_team_id", "batches", ["team_id"])
    op.create_index("ix_batches_status", "batches", ["status"])
    op.create_index(
        "ix_batches_provider_batch_id", "batches", ["provider_batch_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_batches_provider_batch_id", table_name="batches")
    op.drop_index("ix_batches_status", table_name="batches")
    op.drop_index("ix_batches_team_id", table_name="batches")
    op.drop_table("batches")
    with op.batch_alter_table("teams") as batch:
        batch.drop_column("batches_enabled")
