"""team hedge policy columns — request hedging for tail latency

Adds two nullable columns to ``teams`` that together describe the team's
per-request hedging policy:

- ``hedge_delay_ms`` (Float): how long the failover executor waits for the
  primary provider to return before *speculatively* starting an identical
  call against the next provider in the chain. NULL or 0.0 disables
  hedging (existing sequential-failover behaviour). A typical value sits
  around the primary's p50 latency — too low fires hedge on every call
  (wastes upstream tokens), too high never fires (no tail-latency win).

- ``hedge_max_count`` (Integer): at most this many hedge candidates fire
  per request, regardless of chain length. Default 1 — fire one
  alternative, race two providers, return the faster. NULL is treated as
  1. Setting to 0 disables hedging explicitly.

Phase 27 — request hedging. Tail-latency reduction technique
(Dean & Barroso, "The Tail at Scale", CACM 2013). The failover layer
already knows the provider chain; hedging adds a wall-clock-triggered
speculative start instead of waiting for the primary to fail.

Revision ID: 0012
Revises: 0011
Create Date: 2026-05-20
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("teams") as batch:
        batch.add_column(sa.Column("hedge_delay_ms", sa.Float(), nullable=True))
        batch.add_column(sa.Column("hedge_max_count", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("teams") as batch:
        batch.drop_column("hedge_max_count")
        batch.drop_column("hedge_delay_ms")
