"""multi-modal input — per-team image-bytes cap

Phase 41 introduces multi-modal (image) input support on the chat
endpoint. The cap protects against one user running up a $50 call by
attaching a 100 MB base64 image — without a cap, a single malicious
or buggy client can drain the team's monthly budget in one request.

``teams.max_image_bytes`` — Integer, nullable, default NULL = no cap.
The chat handler sums the total base64-payload byte count across all
image parts in the request. When the sum exceeds the cap, the request
is rejected with HTTP 422 BEFORE the upstream provider call is made.
NULL preserves existing behaviour for teams that haven't opted in.

HTTPS image URLs (where the model fetches the image directly) bypass
the cap by design: we'd need to fetch the URL to measure size, and
that's a separate cost / latency concern. Operators wanting to block
URL-based images entirely can use the existing guardrail policy
(``rule_actions": {"image_url": "block"}`` is a future extension —
not in this phase).

Phase 41 — multi-modal input.

Revision ID: 0019
Revises: 0018
Create Date: 2026-05-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("teams") as batch:
        batch.add_column(sa.Column("max_image_bytes", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("teams") as batch:
        batch.drop_column("max_image_bytes")
