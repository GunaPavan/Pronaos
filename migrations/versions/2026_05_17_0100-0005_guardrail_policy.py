"""guardrail_policy — per-team override for ingress/egress rules

Adds a single nullable JSON column ``guardrail_policy`` to ``teams``.
Schema for the JSON value (validated by the CLI / admin endpoint, not
by the DB):

    {
      "disabled_rules":  ["pii.ipv4"],
      "rule_actions": {
        "injection": "block"
      }
    }

NULL means "use the engine's default policy" — fail-safe for tenants
that haven't tuned their guardrails. The chat handler resolves the
effective policy at request time and passes it to the engine on each
scan call. Phase 8.2 in PLAN.md.

Revision ID: 0005
Revises: 0004
Create Date: 2026-05-17
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("teams") as batch:
        batch.add_column(sa.Column("guardrail_policy", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("teams") as batch:
        batch.drop_column("guardrail_policy")
