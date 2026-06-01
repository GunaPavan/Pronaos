"""Dual-auth integration tests (Phase 26).

Proves the request-time path: a JWT-shaped Bearer token goes through
the OIDC verifier; an API-key-shaped token goes through the existing
``verify_key`` path. The two never collide on the wire, so dispatch
is structural (no per-request flag, no per-route config).

Strategy
--------
Reuse the ``auth_setup`` fixture's seeded tenant. Stamp an
``oidc_subject`` on it via direct SQLAlchemy. Stand up an
``OidcVerifier`` with a stub JWKS client that hands back our test
key. Mint a JWT signed with the matching private key. Hit
``/v1/admin/usage`` (admin endpoint, requires ``admin:usage`` scope)
with the JWT in the Bearer header.

The auth_setup fixture's API-key path still works in the same app —
asserted by re-using the existing key for a separate request.
"""

from __future__ import annotations

import time
from typing import Any

import jwt
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import select

from pronaos.auth.oidc import OidcVerifier
from pronaos.db.models import Tenant

# --------------------------------------------------------------------------- #
# Fixtures                                                                    #
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def rsa_keypair() -> tuple[Any, Any]:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private, private.public_key()


@pytest.fixture
def signing_key_pem(rsa_keypair: tuple[Any, Any]) -> bytes:
    private, _ = rsa_keypair
    return private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


class _StubJWKSClient:
    def __init__(self, public_key: Any) -> None:
        self._public_key = public_key

    class _StubSigningKey:
        def __init__(self, key: Any) -> None:
            self.key = key

    def get_signing_key_from_jwt(self, _token: str) -> _StubJWKSClient._StubSigningKey:
        return _StubJWKSClient._StubSigningKey(self._public_key)


def _mint_admin_token(
    signing_key_pem: bytes,
    *,
    subject: str,
    issuer: str = "https://test.idp/realms/pronaos",
    audience: str = "pronaos-gateway",
    expires_in: int = 3600,
) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "iss": issuer,
            "sub": subject,
            "aud": audience,
            "iat": now,
            "exp": now + expires_in,
        },
        signing_key_pem,
        algorithm="RS256",
    )


# --------------------------------------------------------------------------- #
# Happy path: JWT auth succeeds against /v1/admin/usage                       #
# --------------------------------------------------------------------------- #


@respx.mock
@pytest.mark.asyncio
async def test_jwt_with_matching_subject_authenticates(
    auth_setup,
    rsa_keypair: tuple[Any, Any],
    signing_key_pem: bytes,
) -> None:
    """The headline behaviour: stamp a tenant's ``oidc_subject``,
    mint a JWT with that ``sub``, hit an admin endpoint with it.
    The gateway resolves the JWT to that tenant and grants
    ``admin:usage`` scope. No API key needed."""
    _, public = rsa_keypair

    # Stamp the seeded tenant with an OIDC subject + install the
    # verifier on the app.
    sm = auth_setup.sm
    async with sm() as session:
        tenant = (
            await session.execute(select(Tenant).where(Tenant.id == auth_setup.tenant_id))
        ).scalar_one()
        tenant.oidc_subject = "alice@example.com"
        await session.commit()

    # Install the verifier on the live FastAPI app state. We bypass
    # the lifespan re-init by setting state directly.
    auth_setup.client._transport.app.state.oidc_verifier = OidcVerifier(  # type: ignore[attr-defined]
        issuer="https://test.idp/realms/pronaos",
        audience="pronaos-gateway",
        jwks_client=_StubJWKSClient(public),  # type: ignore[arg-type]
    )

    token = _mint_admin_token(signing_key_pem, subject="alice@example.com")

    resp = await auth_setup.client.get(
        "/v1/admin/usage",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 200, resp.text


@respx.mock
@pytest.mark.asyncio
async def test_jwt_with_unknown_subject_returns_401(
    auth_setup,
    rsa_keypair: tuple[Any, Any],
    signing_key_pem: bytes,
) -> None:
    """A JWT verifies cleanly but its ``sub`` isn't claimed by any
    tenant → 401. The gateway never reveals which subjects ARE
    configured (no enumeration vector)."""
    _, public = rsa_keypair

    auth_setup.client._transport.app.state.oidc_verifier = OidcVerifier(  # type: ignore[attr-defined]
        issuer="https://test.idp/realms/pronaos",
        audience="pronaos-gateway",
        jwks_client=_StubJWKSClient(public),  # type: ignore[arg-type]
    )

    # No tenant has oidc_subject = ghost — auth_setup leaves it NULL.
    token = _mint_admin_token(signing_key_pem, subject="ghost@nowhere.example")
    resp = await auth_setup.client.get(
        "/v1/admin/usage",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 401


@respx.mock
@pytest.mark.asyncio
async def test_jwt_expired_returns_401(
    auth_setup,
    rsa_keypair: tuple[Any, Any],
    signing_key_pem: bytes,
) -> None:
    """Expired tokens are rejected by PyJWT regardless of subject
    mapping. The 401 is the operator-side signal "your IdP token
    expired; refresh it."""
    _, public = rsa_keypair
    auth_setup.client._transport.app.state.oidc_verifier = OidcVerifier(  # type: ignore[attr-defined]
        issuer="https://test.idp/realms/pronaos",
        audience="pronaos-gateway",
        jwks_client=_StubJWKSClient(public),  # type: ignore[arg-type]
    )

    token = _mint_admin_token(signing_key_pem, subject="alice@example.com", expires_in=-60)
    resp = await auth_setup.client.get(
        "/v1/admin/usage",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 401


# --------------------------------------------------------------------------- #
# API-key path still works alongside OIDC                                     #
# --------------------------------------------------------------------------- #


@respx.mock
@pytest.mark.asyncio
async def test_api_key_still_works_when_oidc_is_configured(
    auth_setup,
    rsa_keypair: tuple[Any, Any],
) -> None:
    """Phase 26 doesn't break Phase 1. With the OIDC verifier
    installed, an underscore-shaped API key still goes through the
    existing argon2 verify path."""
    _, public = rsa_keypair
    auth_setup.client._transport.app.state.oidc_verifier = OidcVerifier(  # type: ignore[attr-defined]
        issuer="https://test.idp/realms/pronaos",
        audience="pronaos-gateway",
        jwks_client=_StubJWKSClient(public),  # type: ignore[arg-type]
    )
    # The seeded key has chat:write only; the admin endpoint requires
    # admin:usage. We expect 403 (forbidden — auth succeeded but the
    # scope check failed), NOT 401 (auth failed). That difference is
    # the proof that the API-key path ran.
    resp = await auth_setup.client.get(
        "/v1/admin/usage",
        headers={"Authorization": f"Bearer {auth_setup.api_key}"},
    )
    assert resp.status_code == 403


@respx.mock
@pytest.mark.asyncio
async def test_jwt_token_with_oidc_disabled_returns_401(
    auth_setup,
    signing_key_pem: bytes,
) -> None:
    """If the operator hasn't configured an OIDC verifier on the
    app, JWT-shaped tokens are rejected with 401 (never leaks "OIDC
    not configured" as a distinguishable response)."""
    # Ensure no verifier is installed.
    auth_setup.client._transport.app.state.oidc_verifier = None  # type: ignore[attr-defined]
    token = _mint_admin_token(signing_key_pem, subject="alice@example.com")
    resp = await auth_setup.client.get(
        "/v1/admin/usage",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 401
