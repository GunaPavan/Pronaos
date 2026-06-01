"""tool-use-aware routing — per-team tool_use_scores + threshold

Phase 46 composes Phase 45 (BFCL-style tool-use accuracy benchmark)
into Phase 24's quality-aware-cheapest router. The new routing
strategy ``tool-use-aware-cheapest`` filters candidate models by
their stored per-model tool-use accuracy BEFORE picking the cheapest
of what remains, but ONLY when the inbound request carries tools.
Tool-less requests fall through to the existing strategies.

``teams.tool_use_scores`` — JSON, nullable, default NULL.

JSON shape (same as ``quality_scores``):

    {
      "groq/llama-3.3-70b-versatile": {
        "score": 1.0,
        "n_samples": 12,
        "source_eval_id": "2026-05-21T17:02:00Z",
        "ts": "2026-05-21T17:02:00Z"
      },
      "groq/llama-3.1-8b-instant": {
        "score": 0.917,
        "n_samples": 12,
        ...
      }
    }

``teams.tool_use_threshold`` — Float (0..1), nullable, default NULL.
When the strategy is active but no explicit threshold is stored,
the router uses ``DEFAULT_TOOL_USE_THRESHOLD`` (0.9) — higher than
the quality threshold because tool-use sloppiness is operationally
costly (wrong tool args break agent loops).

Phase 46 — tool-use-aware routing.

Revision ID: 0020
Revises: 0019
Create Date: 2026-05-26
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("teams") as batch:
        batch.add_column(sa.Column("tool_use_threshold", sa.Float(), nullable=True))
        batch.add_column(sa.Column("tool_use_scores", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("teams") as batch:
        batch.drop_column("tool_use_scores")
        batch.drop_column("tool_use_threshold")
