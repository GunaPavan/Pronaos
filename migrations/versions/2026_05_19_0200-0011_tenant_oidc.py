"""tenant oidc_subject column — OIDC/SSO admin auth

Adds a nullable ``oidc_subject`` column to ``tenants``. When set, the
gateway accepts an OIDC JWT whose ``sub`` claim matches this value as
admin auth for the tenant — same access as an ``admin:usage`` API key.
NULL on this column means "no OIDC admin for this tenant" — only the
existing API-key path works.

The value is operator-controlled: typically the human admin's email
or OIDC ``sub`` claim from the identity provider (e.g. Keycloak
``preferred_username``, Auth0 ``sub``, Azure AD ``oid``). Validated
by the CLI / admin endpoint, not the DB.

The OIDC issuer + audience are gateway-wide settings
(``PRONAOS_OIDC_ISSUER``, ``PRONAOS_OIDC_AUDIENCE``) — every JWT we
accept must come from the same configured IdP. Per-tenant issuers
are a future phase that needs another column; today we trust one IdP
per gateway deployment, which matches the "one company, one SSO
tenant" reality of every Pronaos customer.

Phase 26 — OIDC/SSO admin auth.

Revision ID: 0011
Revises: 0010
Create Date: 2026-05-19
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("tenants") as batch:
        batch.add_column(sa.Column("oidc_subject", sa.String(255), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("tenants") as batch:
        batch.drop_column("oidc_subject")
