"""tenant webhook columns — operational event delivery

Adds nullable ``webhook_url`` + ``webhook_secret`` columns to ``tenants``.
Both NULL = webhooks disabled (no-op dispatcher). Both set = events are
HTTP-POSTed to ``webhook_url`` with an HMAC-SHA256 signature derived
from ``webhook_secret`` in the ``X-Pronaos-Signature`` header.

Phase 19 — webhook outbound events.

Revision ID: 0008
Revises: 0007
Create Date: 2026-05-18
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("tenants") as batch:
        batch.add_column(sa.Column("webhook_url", sa.String(2048), nullable=True))
        batch.add_column(sa.Column("webhook_secret", sa.String(255), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("tenants") as batch:
        batch.drop_column("webhook_secret")
        batch.drop_column("webhook_url")
