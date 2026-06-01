"""tool-call result caching — per-team enable + TTL

Phase 49 caches tool execution results by ``(team_id, tool_name,
canonical_args_json)``. When a chat request arrives with trailing
``assistant.tool_calls`` awaiting execution, the gateway checks the
cache for each pending call — on hit, a synthetic ``tool`` message
is injected before forwarding so the LLM can synthesise an answer
without the client having to re-execute the tool. The cache is
populated from past chat requests where the client included the
matching ``tool`` role result.

``teams.tool_result_cache_enabled`` — BOOLEAN, NOT NULL, default
FALSE. Opt-in per team — disabled by default because tool-result
caching only makes sense for deterministic-in-args tools
(``get_weather``, ``lookup_user_by_id``, ``fetch_static_doc``).
Tools with side effects (``send_email``, ``delete_record``) or
time-sensitive results (``get_stock_price``) MUST stay uncached;
the team operator owns that policy decision.

``teams.tool_result_cache_ttl_seconds`` — INTEGER, nullable. TTL
applied when records are written. NULL → ``DEFAULT_TTL_SECONDS``
(3600 = 1 hour, conservative since cached tool results age out of
correctness rapidly). Operators can raise to days for very stable
tools or lower to minutes for borderline-cacheable ones.

The cache itself lives in Redis (see ``core.tool_result_cache``) —
team row only carries the operator-tuned thresholds.

Revision ID: 0022
Revises: 0021
Create Date: 2026-05-30
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("teams") as batch:
        batch.add_column(
            sa.Column(
                "tool_result_cache_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )
        batch.add_column(
            sa.Column(
                "tool_result_cache_ttl_seconds",
                sa.Integer(),
                nullable=True,
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("teams") as batch:
        batch.drop_column("tool_result_cache_ttl_seconds")
        batch.drop_column("tool_result_cache_enabled")
