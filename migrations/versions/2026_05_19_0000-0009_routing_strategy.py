"""team routing_strategy column — cost-aware auto-routing

Adds a nullable ``routing_strategy`` column to ``teams``. When the column
is NULL the team has no auto-routing preference (auto-routed requests
default to ``cheapest``). Allowed values are the wire-format strings from
``RoutingStrategy``: ``cheapest`` | ``fastest`` | ``balanced``. Validated
by the CLI / admin endpoint before write, not by the DB — keeping the
column generic future-proofs it for additional strategies (e.g.
``quality``, ``latency-budget``) without a schema change.

Phase 21 — cost-aware routing.

Revision ID: 0009
Revises: 0008
Create Date: 2026-05-19
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("teams") as batch:
        batch.add_column(sa.Column("routing_strategy", sa.String(32), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("teams") as batch:
        batch.drop_column("routing_strategy")
