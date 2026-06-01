"""ab_test config column on teams + ab_arm attribution on usage_records

Two columns ship together because they're paired by use case:

- ``teams.ab_test`` — JSON shape carrying the per-team active A/B test
  configuration. At most one active test per team; a value of NULL
  means no test is running and routing behaves as usual. The shape::

      {
        "id": "<uuid>",
        "name": "haiku-vs-sonnet",
        "started_at": "2026-05-20T18:00:00+00:00",
        "arm_a": {"model": "anthropic/claude-3-5-haiku", "weight": 0.5},
        "arm_b": {"model": "anthropic/claude-3-5-sonnet", "weight": 0.5}
      }

  The gateway substitutes a request's model on a per-call deterministic
  hash bucket so retries of the same logical request land in the same
  arm.

- ``usage_records.ab_arm`` — String column tagging each usage row with
  the arm the call was attributed to. ``"a"``, ``"b"``, or NULL when
  the call was not part of an A/B test (the common case). The
  ``abtest report`` CLI aggregates per-arm by filtering on this column.

Phase 29 — A/B testing harness.

Revision ID: 0013
Revises: 0012
Create Date: 2026-05-20
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("teams") as batch:
        batch.add_column(sa.Column("ab_test", sa.JSON(), nullable=True))
    with op.batch_alter_table("usage_records") as batch:
        batch.add_column(sa.Column("ab_arm", sa.String(4), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("usage_records") as batch:
        batch.drop_column("ab_arm")
    with op.batch_alter_table("teams") as batch:
        batch.drop_column("ab_test")
