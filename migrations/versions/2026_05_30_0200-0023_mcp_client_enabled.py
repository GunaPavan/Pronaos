"""MCP client federation — per-team enable flag

Phase 54 closes the bidirectional MCP narrative: Pronaos was an MCP
**server** (Phases 48-51); now it's also an MCP **client** that
federates external MCP servers into chat completions.

``teams.mcp_client_enabled`` — BOOLEAN, NOT NULL, default FALSE.
Opt-in per team because stdio MCP servers spawn subprocesses on the
gateway host (security-sensitive — arbitrary command execution).
The flag gates whether a team's chat requests can reference MCP
servers via ``body.pronaos_mcp_servers``. Disabled teams that pass
the field get a 422 with an explicit ``mcp_client_disabled`` detail.

v1 ships no per-server allowlist; the per-team flag is the only
policy lever. A future phase can add a fine-grained allowlist
(``mcp_client_allowed_commands`` or similar) for multi-tenant
deployments where some teams should be restricted to a curated set
of MCP servers.

Revision ID: 0023
Revises: 0022
Create Date: 2026-05-30
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("teams") as batch:
        batch.add_column(
            sa.Column(
                "mcp_client_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("teams") as batch:
        batch.drop_column("mcp_client_enabled")
