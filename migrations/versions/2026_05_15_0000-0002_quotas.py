"""quotas — rps_limit + monthly_token_budget + period tracking

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-15
"""

from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def _next_period_reset_iso() -> str:
    """Default value for existing rows: first day of next month UTC, ISO 8601."""
    now = datetime.now(tz=UTC)
    year, month = now.year, now.month
    if month == 12:
        year += 1
        month = 1
    else:
        month += 1
    return datetime(year, month, 1, 0, 0, 0, tzinfo=UTC).isoformat()


def upgrade() -> None:
    # ---- teams: budget tracking ----
    with op.batch_alter_table("teams") as batch:
        batch.add_column(sa.Column("monthly_token_budget", sa.BigInteger(), nullable=True))
        # current_period_tokens defaults to 0 — server_default lets us add it
        # to existing rows without a default-violation error on SQLite.
        batch.add_column(
            sa.Column(
                "current_period_tokens",
                sa.BigInteger(),
                nullable=False,
                server_default="0",
            )
        )
        batch.add_column(
            sa.Column(
                "period_resets_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=_next_period_reset_iso(),
            )
        )

    # ---- api_keys: per-key RPS cap ----
    with op.batch_alter_table("api_keys") as batch:
        batch.add_column(sa.Column("rps_limit", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("api_keys") as batch:
        batch.drop_column("rps_limit")
    with op.batch_alter_table("teams") as batch:
        batch.drop_column("period_resets_at")
        batch.drop_column("current_period_tokens")
        batch.drop_column("monthly_token_budget")
