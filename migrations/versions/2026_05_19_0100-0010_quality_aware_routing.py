"""team quality_threshold + quality_scores columns — quality-aware routing

Adds two nullable columns to ``teams`` that wire the eval harness
(Phase 23 multi-judge) into the cost-aware router (Phase 21):

- ``quality_threshold`` (Float, nullable; default 0.7 when the
  ``quality-aware-cheapest`` strategy is in use but the column is
  NULL — keeping the column NULLable lets teams opt out of quality
  filtering even when the team-wide threshold is unset elsewhere).
- ``quality_scores`` (JSON, nullable) — dict keyed by fully-qualified
  model name (``provider/model``) mapping to ``{"score": float,
  "n_samples": int, "source_eval_id": str, "ts": iso-8601}``.
  Populated by ``pronaos-cli eval store-scores``; NULL means "no
  recent eval data, fall back to pure cheapest selection."

Phase 24 — quality-aware routing.

Revision ID: 0010
Revises: 0009
Create Date: 2026-05-19
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("teams") as batch:
        batch.add_column(sa.Column("quality_threshold", sa.Float(), nullable=True))
        batch.add_column(sa.Column("quality_scores", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("teams") as batch:
        batch.drop_column("quality_scores")
        batch.drop_column("quality_threshold")
