"""allowed_models — per-team model allowlist for least-privilege multi-tenancy

Adds a nullable JSON column ``allowed_models`` to ``teams``. Schema:

    ["groq/*", "anthropic/claude-opus-*", "groq/llama-3.3-70b-versatile"]

Entries are glob patterns matched against the request's ``model`` field
(post-prefix). ``*`` matches any sequence of characters EXCEPT ``/``;
``**`` matches any sequence including ``/``. Exact-match entries are
the common case.

NULL means "no restriction" — backwards-compatible default for existing
teams. The chat handler reads the principal's ``allowed_models`` after
auth and returns 403 if the requested model doesn't match any pattern.
Phase 16-extension in PLAN.md (model governance).

Revision ID: 0007
Revises: 0006
Create Date: 2026-05-18
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("teams") as batch:
        batch.add_column(sa.Column("allowed_models", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("teams") as batch:
        batch.drop_column("allowed_models")
