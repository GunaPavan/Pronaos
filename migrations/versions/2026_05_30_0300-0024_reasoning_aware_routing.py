"""reasoning-aware routing — per-team thresholds for the new strategy

Phase 57 composes Phase 56 (reasoning-token extraction across direct
Anthropic / OpenAI / DeepSeek / Bedrock / Vertex Gemini + Anthropic)
into Phase 47's routing scaffold. The new strategy
``reasoning-aware-cheapest`` ranks eligible models by their
*expected* effective cost given a per-model rolling observation of
the team's reasoning-token ratio.

Storage shape mirrors Phase 47's prompt-cache observations: per
``(team_id, fqmn)`` rolling totals live in Redis (the
``ReasoningObserver`` writes ``completion_tokens`` + ``reasoning_tokens``
on every chat response that surfaces a non-zero reasoning count).
The Team row only carries the two thresholds an operator wants to
tune:

``teams.reasoning_aware_min_samples`` — INTEGER, nullable, default
NULL. Minimum sample count an fqmn must have accumulated before its
observed reasoning ratio is trusted by the router. Below this, the
router treats the fqmn as "unobserved" and falls back to plain
``cheapest`` cost math for it. When NULL the router uses
``DEFAULT_REASONING_MIN_SAMPLES`` (20).

``teams.reasoning_aware_max_ratio`` — FLOAT (0..1), nullable, default
NULL. Optional **safety cap**: models whose observed ratio exceeds
this value are EXCLUDED from the candidate pool entirely (treated
as too reasoning-heavy regardless of base price). When NULL the
router applies no exclusion cap and ranks purely by effective cost.

The strategy's math is symmetric with Phase 47:

    effective_output_rate = nominal_output_rate * (1 + observed_ratio)

where ``observed_ratio = reasoning_tokens / completion_tokens``. So
a model that burns 50% of its output on reasoning gets its output
rate scored as 1.5x nominal before the cheapest-of-survivors pick.
Models with no observed reasoning (regression for plain
non-reasoning models on this team's traffic) are scored at 1.0x
exactly — no behavioural change.

Revision ID: 0024
Revises: 0023
Create Date: 2026-05-30
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("teams") as batch:
        batch.add_column(sa.Column("reasoning_aware_min_samples", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("reasoning_aware_max_ratio", sa.Float(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("teams") as batch:
        batch.drop_column("reasoning_aware_max_ratio")
        batch.drop_column("reasoning_aware_min_samples")
