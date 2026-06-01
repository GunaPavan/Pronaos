"""reversible PII tokenization

Two new team-level columns ship together for Phase 38:

- ``teams.pii_tokenization_enabled`` — boolean, default False. When True,
  the guardrail engine emits ``TOKENIZE`` actions (instead of ``REDACT``)
  for PII rules whose per-team ``rule_actions`` map them to that action.
  Default False preserves existing behaviour — teams that haven't opted
  in keep the one-way redaction path.

- ``teams.pii_token_ttl_seconds`` — Integer, nullable. How long the
  (token → original) mapping persists in Redis after the ingress write.
  NULL falls back to the gateway default (3600s / 1 hour). Tight TTLs
  reduce the PII-at-rest window; loose TTLs let long agent loops still
  reverse tokens across many turns.

Why tokenization (a new action, not a new rule):

  Existing REDACT replaces matched PII with a generic marker
  (``[REDACTED-EMAIL]``). The substitution is one-way and lossy — the
  upstream LLM can't reason about an entity it can never see, and the
  client never gets the original back. Claim #3 already showed this
  breaks topically-relevant cases.

  TOKENIZE replaces matched PII with a deterministic, per-tenant-salted
  token (``[EMAIL_a3f7c2e1b890]``). The token is reversible — the
  gateway holds the mapping in Redis and reverses it in the response
  before returning to the client. The LLM still never sees the original
  (compliance preserved), but two mentions of the same value become
  the same token (entity tracking preserved), and the client sees the
  original data restored.

  This is the same shape as classical pseudonymization in privacy law:
  the upstream sees pseudonyms, the gateway holds the key, the client
  sees the real data.

Phase 38 — reversible PII tokenization.

Revision ID: 0016
Revises: 0015
Create Date: 2026-05-22
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("teams") as batch:
        batch.add_column(
            sa.Column(
                "pii_tokenization_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch.add_column(
            sa.Column("pii_token_ttl_seconds", sa.Integer(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("teams") as batch:
        batch.drop_column("pii_token_ttl_seconds")
        batch.drop_column("pii_tokenization_enabled")
