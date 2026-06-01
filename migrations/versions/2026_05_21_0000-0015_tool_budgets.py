"""tool-call observability + per-tool budgets

Three columns ship together for Phase 37:

- ``usage_records.tool_names`` — Text, nullable. Comma-separated list of
  tool names the LLM emitted in this call's response (e.g.
  "web_search,fetch_url"). NULL for calls that didn't emit tool_calls
  and for legacy rows. Lets ``pronaos-cli team chargeback`` slice
  spend by tool — "this team spent $X on calls that triggered
  web_search."
- ``audit_records.tool_names`` — Text, nullable. Same shape on the
  audit chain. Per-call queryable for "which tools did the agent call
  during this audit window."
- ``teams.tool_budgets`` — JSON, nullable. Per-tool monthly cap shape:

    {
      "web_search":  {"limit_calls": 100, "current_calls": 23},
      "code_exec":   {"limit_calls": 50,  "current_calls": 5}
    }

  ``limit_calls`` is the operator-configured cap. ``current_calls`` is
  the running count for the current period; resets together with the
  team's monthly_token_budget rollover so all budget counters share
  one calendar-month boundary. NULL means no per-tool caps for this
  team — existing behaviour preserved.

Phase 37 — tool-call observability + per-tool budgets.

Revision ID: 0015
Revises: 0014
Create Date: 2026-05-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("usage_records") as batch:
        batch.add_column(sa.Column("tool_names", sa.Text(), nullable=True))
    with op.batch_alter_table("audit_records") as batch:
        batch.add_column(sa.Column("tool_names", sa.Text(), nullable=True))
    with op.batch_alter_table("teams") as batch:
        batch.add_column(sa.Column("tool_budgets", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("teams") as batch:
        batch.drop_column("tool_budgets")
    with op.batch_alter_table("audit_records") as batch:
        batch.drop_column("tool_names")
    with op.batch_alter_table("usage_records") as batch:
        batch.drop_column("tool_names")
