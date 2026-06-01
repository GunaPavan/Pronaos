"""OIDC JWT verification for human admin access (Phase 26).

Why this exists
---------------
Pronaos's existing auth path is API-key based — server-to-server. Human
admins have no SSO story, which is the single biggest blocker for
enterprise procurement (every regulated buyer asks "how do humans log
in?"). Phase 26 adds a parallel Bearer-token path that accepts an OIDC
JWT from a configured identity provider (Keycloak, Auth0, Azure AD,
Google, etc.) and resolves it to a tenant admin Principal.

Trust model
-----------
- **One IdP per gateway deployment** (``PRONAOS_OIDC_ISSUER``). The
  realistic case for almost every Pronaos customer: one company,
  one identity provider. Multi-IdP-per-deployment is a future
  phase that needs a per-tenant issuer column.
- **Per-tenant ``oidc_subject``** maps the IdP's ``sub`` claim
  (or a deterministic equivalent like ``preferred_username``) to a
  tenant. A JWT whose ``sub`` doesn't match any tenant's
  ``oidc_subject`` is rejected.
- **JWKS verification** — public-key signatures fetched from the
  IdP's standard ``/.well-known/jwks.json``. We never see the
  IdP's private key.
- **Audience pinning** (optional) via ``PRONAOS_OIDC_AUDIENCE``.
  Belt-and-braces against token reuse across services.
- **Standard claim checks**: ``iss``, ``aud`` (if pinned), ``exp``,
  ``nbf``. PyJWT enforces all of these by default.

Caching
-------
``PyJWKClient`` caches the JWKS in-memory with a default 5-minute
TTL. That's the right amount of freshness for routine key rotation
events; an attacker who got a key revoked would have at most 5 min
of remaining validity for tokens minted before the revoke landed
in the JWKS.

Fail-closed
-----------
This module's API is "verify or raise." Any failure — network
glitch fetching JWKS, bad signature, expired token, unknown
``sub`` — surfaces as a single ``OidcAuthError``. The dual-auth
middleware in ``deps.py`` decides what HTTP status to return
(401 for OIDC failures, fall-through to the API-key path doesn't
apply since the JWT format is unambiguous on the wire).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

import jwt
from jwt import PyJWKClient
from jwt.exceptions import (
    InvalidAudienceError,
    InvalidIssuerError,
    InvalidSignatureError,
    InvalidTokenError,
)

from pronaos.logging import get_logger

log = get_logger(__name__)

# Signing algorithms we accept. RS256 is the OIDC standard; PS256
# and ES256 are common modern alternatives. Symmetric (HS256) is
# deliberately excluded — JWKS verification only makes sense with
# asymmetric signatures.
_ALLOWED_ALGS: Final[list[str]] = ["RS256", "RS384", "RS512", "ES256", "ES384", "PS256"]


class OidcAuthError(Exception):
    """Verification failed. Surfaces as HTTP 401 at the auth layer.

    Carries a short, non-leaking reason string so logs can be useful
    without exposing token content to attackers (e.g. don't log the
    JWT payload — just the failure category).
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class OidcClaims:
    """The subset of OIDC claims Pronaos cares about post-verification."""

    issuer: str
    subject: str
    audience: str | list[str] | None
    expires_at: int  # unix seconds
    raw: dict[str, Any]  # full payload — useful for downstream extensions


class OidcVerifier:
    """Verifies inbound OIDC JWTs against a configured issuer + JWKS.

    Construction is cheap — JWKS fetches are lazy via PyJWKClient and
    cached for 5 minutes. Tests can pass a stub jwks_client to bypass
    the network entirely.
    """

    def __init__(
        self,
        *,
        issuer: str,
        audience: str | None = None,
        jwks_url: str | None = None,
        jwks_client: PyJWKClient | None = None,
    ) -> None:
        self._issuer = issuer
        self._audience = audience
        # Build the JWKS client from either the explicit URL, the
        # supplied client (test override), or the discovery default.
        # We don't fetch the discovery doc at construction — that
        # would block startup on a flaky IdP. Just point at the
        # well-known JWKS endpoint, which every conformant IdP
        # publishes at a predictable path.
        if jwks_client is not None:
            self._jwks_client = jwks_client
        else:
            effective_jwks = jwks_url or f"{issuer.rstrip('/')}/protocol/openid-connect/certs"
            # Note: Keycloak's JWKS lives under
            # ``/realms/{realm}/protocol/openid-connect/certs``; Auth0
            # uses ``/.well-known/jwks.json``; Google uses
            # ``/oauth2/v3/certs``. ``jwks_url`` config explicitly
            # overrides for IdPs whose layout differs from Keycloak's.
            self._jwks_client = PyJWKClient(effective_jwks, cache_keys=True)

    def verify(self, token: str) -> OidcClaims:
        """Validate a JWT. Returns the parsed claims, or raises
        :class:`OidcAuthError` with a short reason on failure.

        The PyJWT call enforces signature + expiry + issuer + audience
        in one go. We map each specific exception to a reason string
        so logs can distinguish "expired" from "wrong issuer" from
        "bad signature" without exposing token internals.
        """
        try:
            signing_key = self._jwks_client.get_signing_key_from_jwt(token).key
        except Exception as e:
            # JWKS fetch / parse failure. Could be a network glitch
            # (we don't retry — caller decides) or a malformed token
            # missing a ``kid`` header.
            log.warning("oidc.jwks_lookup_failed", error=str(e))
            raise OidcAuthError("jwks_lookup_failed") from e

        # PyJWT's audience handling: passing ``audience=None`` means
        # "the token must NOT have an aud claim." That's the wrong
        # semantics for us — when the operator hasn't pinned an
        # audience we want to ACCEPT any aud (or none). So we pass
        # the explicit ``verify_aud: False`` option in that case.
        decode_options: dict[str, Any] = {"require": ["exp", "iss", "sub"]}
        if self._audience is None:
            decode_options["verify_aud"] = False
        try:
            payload = jwt.decode(
                token,
                signing_key,
                algorithms=_ALLOWED_ALGS,
                issuer=self._issuer,
                audience=self._audience,
                options=decode_options,  # type: ignore[arg-type]
            )
        except InvalidSignatureError as e:
            raise OidcAuthError("invalid_signature") from e
        except InvalidIssuerError as e:
            raise OidcAuthError("invalid_issuer") from e
        except InvalidAudienceError as e:
            raise OidcAuthError("invalid_audience") from e
        except jwt.ExpiredSignatureError as e:
            raise OidcAuthError("token_expired") from e
        except InvalidTokenError as e:
            # Catch-all for the other PyJWT validation errors
            # (immature signature, missing claim, bad format).
            raise OidcAuthError(f"invalid_token: {e}") from e

        sub = payload.get("sub")
        if not isinstance(sub, str) or not sub:
            raise OidcAuthError("missing_sub_claim")
        iss = payload.get("iss")
        if not isinstance(iss, str) or not iss:
            raise OidcAuthError("missing_iss_claim")
        exp = payload.get("exp")
        if not isinstance(exp, int):
            raise OidcAuthError("missing_exp_claim")

        return OidcClaims(
            issuer=iss,
            subject=sub,
            audience=payload.get("aud"),
            expires_at=exp,
            raw=payload,
        )
