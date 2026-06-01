"""structured output validation + auto-retry

Two new team-level columns for Phase 39:

- ``teams.structured_output_max_retries`` — Integer, default 2. Cap on
  how many times the gateway re-fires a completion when the LLM's
  response fails JSON Schema validation. ``0`` disables auto-retry
  (validation still runs; failures surface to the client as a header).
  The default of 2 covers the common case (most schema violations
  resolve within one retry given a corrective prompt) without
  unbounded cost amplification.

- ``teams.structured_output_provider_native`` — Boolean, default True.
  When True, the chat handler forwards a client-supplied JSON Schema
  to the upstream provider's native structured-output mechanism
  (OpenAI ``response_format: {type:"json_schema", ...}``) where
  available. When False, the gateway falls back to schema-guided
  prompting on every provider (works everywhere, slightly higher
  violation rate). Operators may flip this when a provider's native
  implementation has known bugs.

Why these two settings ship together:

  ``provider_native`` controls HOW the schema reaches the LLM;
  ``max_retries`` controls WHAT to do when the LLM still violates
  it. Both decisions are per-team because different workloads have
  different cost/quality trade-offs: a high-volume classifier can
  afford zero retries (treat violations as bugs and surface them),
  while a low-volume contract-extraction job might prefer 3 retries
  to maximise success rate.

Phase 39 — gateway-side structured-output validation with auto-retry.

Revision ID: 0017
Revises: 0016
Create Date: 2026-05-23
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("teams") as batch:
        batch.add_column(
            sa.Column(
                "structured_output_max_retries",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("2"),
            )
        )
        batch.add_column(
            sa.Column(
                "structured_output_provider_native",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("teams") as batch:
        batch.drop_column("structured_output_provider_native")
        batch.drop_column("structured_output_max_retries")
