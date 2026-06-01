"""prompt-cache-aware routing — per-team thresholds for the new strategy

Phase 47 composes Phases 34/35 (prompt-cache extraction from Anthropic +
OpenAI responses) into Phase 46's routing scaffold. The new strategy
``prompt-cache-aware-cheapest`` ranks eligible models by their
*expected* cost given a per-model rolling observation of the team's
prompt-cache hit rate.

Stats live in Redis (the ``PromptCacheObserver`` writes a rolling
total of ``cached_input_tokens / prompt_input_tokens`` per ``(team_id,
fqmn)``), NOT on the Team row — they change continuously with traffic.
The Team row only carries the two thresholds an operator wants to tune:

``teams.prompt_cache_min_samples`` — INTEGER, nullable, default NULL.
Minimum sample count an fqmn must have accumulated before its
observed hit rate is trusted by the router. Below this, the router
treats the fqmn as "unobserved" and falls back to plain
``cheapest`` cost math for it. When NULL the router uses
``DEFAULT_PROMPT_CACHE_MIN_SAMPLES`` (20).

``teams.prompt_cache_min_hit_rate`` — FLOAT (0..1), nullable, default
NULL. Floor on the cache hit rate; models whose observed rate is
below this are scored on plain cost (the cache-savings adjustment
is too small to be load-bearing). When NULL the router uses
``DEFAULT_PROMPT_CACHE_MIN_HIT_RATE`` (0.1 = 10% of input tokens
must be hitting cache before the strategy adjusts the model's
cost score).

Together: a team that hasn't run enough traffic through a model
sees that model's cost ranked by raw pricing; a team that HAS run
enough traffic gets a model's effective cost discounted by the
observed cache savings before the cheapest-of-survivors pick. The
strategy degrades to plain ``cheapest`` when no fqmn clears both
thresholds.

Revision ID: 0021
Revises: 0020
Create Date: 2026-05-30
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("teams") as batch:
        batch.add_column(sa.Column("prompt_cache_min_samples", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("prompt_cache_min_hit_rate", sa.Float(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("teams") as batch:
        batch.drop_column("prompt_cache_min_hit_rate")
        batch.drop_column("prompt_cache_min_samples")
