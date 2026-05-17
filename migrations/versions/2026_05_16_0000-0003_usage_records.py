"""usage records — per-call FinOps + audit data

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-16
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "usage_records",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column(
            "ts",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        # Soft references — no FK so usage history survives tenant/team/key deletion
        sa.Column("tenant_id", sa.String(length=32), nullable=False),
        sa.Column("team_id", sa.String(length=32), nullable=False),
        sa.Column("key_id", sa.String(length=32), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=255), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completion_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost_hcents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="success"),
    )
    op.create_index(
        "ix_usage_records_team_ts", "usage_records", ["team_id", "ts"]
    )
    op.create_index(
        "ix_usage_records_tenant_ts", "usage_records", ["tenant_id", "ts"]
    )


def downgrade() -> None:
    op.drop_index("ix_usage_records_tenant_ts", table_name="usage_records")
    op.drop_index("ix_usage_records_team_ts", table_name="usage_records")
    op.drop_table("usage_records")
