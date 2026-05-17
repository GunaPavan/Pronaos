"""cost_budget — per-team monthly cost cap, denominated in hundredths-of-a-cent

Adds two columns to ``teams`` parallel to the existing token-budget columns:

- ``monthly_cost_hcents_budget`` — int, NULL = unlimited.
- ``current_period_cost_hcents`` — running counter incremented after each
  successful provider call by ``UsageRecord.cost_hcents``.

The existing ``period_resets_at`` column on ``teams`` (added in 0002) governs
the calendar-month rollover for BOTH the token and cost counters; we don't
need a parallel timestamp.

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-17
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("teams") as batch:
        batch.add_column(
            sa.Column("monthly_cost_hcents_budget", sa.BigInteger(), nullable=True)
        )
        # Server-default 0 so existing rows acquire the column without violating
        # the NOT NULL constraint at migration time (SQLite would otherwise
        # complain on backfill).
        batch.add_column(
            sa.Column(
                "current_period_cost_hcents",
                sa.BigInteger(),
                nullable=False,
                server_default="0",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("teams") as batch:
        batch.drop_column("current_period_cost_hcents")
        batch.drop_column("monthly_cost_hcents_budget")
