"""quality regression detection + auto-routing

Closed-loop quality monitoring. Phase 40 adds:

- ``quality_samples`` table — append-only log of LLM-judge scores for
  sampled production responses. One row per sample: team_id, model,
  request_id, score (0..1), ts. Operators query this for "show me
  model X's quality trend over the last 24 hours".

- ``teams.quality_sampling_rate`` — float, default 0.0 = off. Per-team
  fraction of production responses that get sampled and judged.
  Operators tune this against the team's chat volume + judge cost
  budget. 0.01 = 1% sampling is typical.

- ``teams.quality_judge_model`` — string, nullable. The model used to
  score samples. NULL falls back to the gateway-wide default (set via
  config). Typically a cheap, fast model (gpt-4o-mini, Claude Haiku)
  so judging doesn't dominate cost.

- ``teams.model_degradation_state`` — JSON, nullable. Per-model
  degradation status. Shape:

      {
        "groq/llama-3.1-8b-instant": {
          "degraded": true,
          "since_ts": "2026-05-24T08:30:00Z",
          "baseline_mean": 0.92,
          "recent_mean": 0.41,
          "n_recent": 25,
          "p_value": 0.0001
        }
      }

  The quality-aware scorer reads this and excludes any model with
  ``degraded: true`` from the auto-routing candidate pool. When the
  next monitor check shows the model has recovered (recent batch no
  longer significantly worse than baseline), ``degraded`` flips to
  false and the model returns to the pool.

Indexes are deliberately tight — the hot read path is "give me the
last N samples for (team, model)" which the ``(team_id, model, ts)``
composite index serves cheaply.

Phase 40 — quality regression detection + automated rerouting.

Revision ID: 0018
Revises: 0017
Create Date: 2026-05-24
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---- quality_samples table ----
    op.create_table(
        "quality_samples",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("tenant_id", sa.String(32), nullable=False),
        sa.Column("team_id", sa.String(32), nullable=False),
        sa.Column("model", sa.String(255), nullable=False),
        sa.Column("request_id", sa.String(64), nullable=True),
        # Score in [0, 1] from the LLM judge. We use Float (not Decimal)
        # because precision beyond 3 decimals is below judge noise floor.
        sa.Column("score", sa.Float(), nullable=False),
        # ``judge_model`` is the model that DID the scoring (not the
        # model being scored). Stored so operators can audit "which
        # judge said this" when a team's quality numbers look odd.
        sa.Column("judge_model", sa.String(255), nullable=False),
    )
    # The hot path: "fetch last N samples for (team, model) ordered by ts desc."
    op.create_index(
        "ix_quality_samples_team_model_ts",
        "quality_samples",
        ["team_id", "model", "ts"],
    )
    # Per-tenant aggregations (FinOps dashboards).
    op.create_index(
        "ix_quality_samples_tenant_ts",
        "quality_samples",
        ["tenant_id", "ts"],
    )

    # ---- Team columns ----
    with op.batch_alter_table("teams") as batch:
        batch.add_column(
            sa.Column(
                "quality_sampling_rate",
                sa.Float(),
                nullable=False,
                server_default=sa.text("0.0"),
            )
        )
        batch.add_column(
            sa.Column("quality_judge_model", sa.String(255), nullable=True)
        )
        batch.add_column(
            sa.Column("model_degradation_state", sa.JSON(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("teams") as batch:
        batch.drop_column("model_degradation_state")
        batch.drop_column("quality_judge_model")
        batch.drop_column("quality_sampling_rate")
    op.drop_index("ix_quality_samples_tenant_ts", table_name="quality_samples")
    op.drop_index("ix_quality_samples_team_model_ts", table_name="quality_samples")
    op.drop_table("quality_samples")
