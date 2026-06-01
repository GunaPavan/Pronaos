"""agent-turn budget columns on teams

A "turn" is a multi-call agent execution (search → summarise → search
again → respond, or similar tool-using loop) keyed by a client-supplied
``X-Pronaos-Agent-Turn-ID`` header. The team has monthly token + cost
budgets (Phase 4/5.7) but no way to cap a SINGLE agent execution —
which is the actual failure mode that burns through a team's whole
monthly budget in one runaway loop.

Three columns ship together:

- ``agent_turn_budget_tokens`` — Integer, nullable. Cumulative
  prompt+completion tokens allowed under one turn-id. NULL =
  unlimited (existing behaviour preserved when the column is absent).
- ``agent_turn_budget_cost_hcents`` — Integer, nullable. Same shape
  for cost; either gate can deny independently.
- ``agent_turn_ttl_seconds`` — Integer, nullable, default 3600.
  How long the per-turn counters persist in Redis. Long enough to
  span a typical agent execution (minutes), short enough that a
  client that forgets to rotate turn-ids doesn't get budget held
  forever.

Phase 30 — agent-turn budget gates.

Revision ID: 0014
Revises: 0013
Create Date: 2026-05-20
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("teams") as batch:
        batch.add_column(sa.Column("agent_turn_budget_tokens", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("agent_turn_budget_cost_hcents", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("agent_turn_ttl_seconds", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("teams") as batch:
        batch.drop_column("agent_turn_ttl_seconds")
        batch.drop_column("agent_turn_budget_cost_hcents")
        batch.drop_column("agent_turn_budget_tokens")
