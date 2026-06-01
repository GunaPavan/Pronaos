"""OIDC JWT verification tests (Phase 26).

Strategy
--------
We mint real RSA-signed JWTs in-test, then verify them through the
``OidcVerifier`` with a stub :class:`jwt.PyJWKClient` that hands back
our test key. This exercises the real PyJWT code path — signature
verification, ``iss`` / ``aud`` / ``exp`` checks, ``alg`` restriction
— without a network hop to a real IdP.

Token mints use ``cryptography`` + ``PyJWT`` directly (both are
already test deps). The fixture key is regenerated per test session
so no one's tempted to reuse it as a credential.
"""

from __future__ import annotations

import time
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from pronaos.auth.oidc import OidcAuthError, OidcVerifier

# --------------------------------------------------------------------------- #
# Fixtures                                                                    #
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="session")
def rsa_keypair() -> tuple[Any, Any]:
    """One RSA keypair for every test in this file. Generated fresh
    per test session; never written to disk."""
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private, private.public_key()


@pytest.fixture
def signing_key_pem(rsa_keypair: tuple[Any, Any]) -> bytes:
    """Private key as PEM bytes — what ``jwt.encode`` wants."""
    private, _ = rsa_keypair
    return private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


class _StubJWKSClient:
    """Stand-in for ``jwt.PyJWKClient`` that returns a fixed public key.

    Real ``PyJWKClient`` fetches a JWKS from the issuer, parses it,
    and finds the key whose ``kid`` matches the token header. For
    tests we just hand back the one public key we minted with —
    no network, no JSON parsing.
    """

    def __init__(self, public_key: Any) -> None:
        self._public_key = public_key

    class _StubSigningKey:
        def __init__(self, key: Any) -> None:
            self.key = key

    def get_signing_key_from_jwt(self, _token: str) -> _StubJWKSClient._StubSigningKey:
        return _StubJWKSClient._StubSigningKey(self._public_key)


@pytest.fixture
def stub_verifier(rsa_keypair: tuple[Any, Any]) -> OidcVerifier:
    """An ``OidcVerifier`` pre-wired with our test JWKS stub."""
    _, public = rsa_keypair
    return OidcVerifier(
        issuer="https://test.idp/realms/pronaos",
        audience="pronaos-gateway",
        jwks_client=_StubJWKSClient(public),  # type: ignore[arg-type]
    )


def _mint_token(
    signing_key_pem: bytes,
    *,
    issuer: str = "https://test.idp/realms/pronaos",
    subject: str = "alice@example.com",
    audience: str | list[str] | None = "pronaos-gateway",
    expires_in: int = 3600,
    extra: dict[str, Any] | None = None,
) -> str:
    """Mint a JWT signed with our test private key. All optional
    claims default to the values ``stub_verifier`` is configured to
    accept, so a happy-path call mints a valid token in one line."""
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": issuer,
        "sub": subject,
        "iat": now,
        "exp": now + expires_in,
    }
    if audience is not None:
        claims["aud"] = audience
    if extra:
        claims.update(extra)
    return jwt.encode(claims, signing_key_pem, algorithm="RS256")


# --------------------------------------------------------------------------- #
# Happy path                                                                  #
# --------------------------------------------------------------------------- #


class TestHappyPath:
    def test_valid_token_verifies(
        self, stub_verifier: OidcVerifier, signing_key_pem: bytes
    ) -> None:
        token = _mint_token(signing_key_pem)
        claims = stub_verifier.verify(token)
        assert claims.subject == "alice@example.com"
        assert claims.issuer == "https://test.idp/realms/pronaos"
        assert claims.audience == "pronaos-gateway"
        # Expiry was 3600s from now ± a few seconds for clock skew.
        assert claims.expires_at > int(time.time())

    def test_raw_payload_carries_custom_claims(
        self, stub_verifier: OidcVerifier, signing_key_pem: bytes
    ) -> None:
        """Custom claims survive verification so downstream consumers
        (audit log, RBAC) can read them via ``claims.raw``."""
        token = _mint_token(
            signing_key_pem, extra={"preferred_username": "alice", "groups": ["admins"]}
        )
        claims = stub_verifier.verify(token)
        assert claims.raw["preferred_username"] == "alice"
        assert claims.raw["groups"] == ["admins"]


# --------------------------------------------------------------------------- #
# Failure paths                                                               #
# --------------------------------------------------------------------------- #


class TestRejections:
    def test_expired_token_rejected(
        self, stub_verifier: OidcVerifier, signing_key_pem: bytes
    ) -> None:
        token = _mint_token(signing_key_pem, expires_in=-60)
        with pytest.raises(OidcAuthError) as exc:
            stub_verifier.verify(token)
        assert exc.value.reason == "token_expired"

    def test_wrong_issuer_rejected(
        self, stub_verifier: OidcVerifier, signing_key_pem: bytes
    ) -> None:
        token = _mint_token(signing_key_pem, issuer="https://attacker.com/realm/pwn")
        with pytest.raises(OidcAuthError) as exc:
            stub_verifier.verify(token)
        assert exc.value.reason == "invalid_issuer"

    def test_wrong_audience_rejected(
        self, stub_verifier: OidcVerifier, signing_key_pem: bytes
    ) -> None:
        token = _mint_token(signing_key_pem, audience="some-other-service")
        with pytest.raises(OidcAuthError) as exc:
            stub_verifier.verify(token)
        assert exc.value.reason == "invalid_audience"

    def test_bad_signature_rejected(
        self,
        stub_verifier: OidcVerifier,
        signing_key_pem: bytes,
    ) -> None:
        """Mint with the test key but tamper with the signature
        segment. PyJWT's verify must catch this."""
        token = _mint_token(signing_key_pem)
        head, payload, _sig = token.split(".")
        tampered = f"{head}.{payload}.QUFBQUFBQUFBQUFBQUFBQUE"
        with pytest.raises(OidcAuthError) as exc:
            stub_verifier.verify(tampered)
        assert exc.value.reason == "invalid_signature"

    def test_missing_sub_rejected(
        self,
        rsa_keypair: tuple[Any, Any],
        signing_key_pem: bytes,
    ) -> None:
        """A token with no ``sub`` claim can't be mapped to a tenant.
        We require ``sub`` even if PyJWT's standard checks pass."""
        # ``options.require`` enforces sub, so PyJWT itself raises
        # MissingRequiredClaimError → InvalidTokenError. Our wrapper
        # translates to invalid_token:....
        _, public = rsa_keypair
        verifier = OidcVerifier(
            issuer="https://test.idp/realms/pronaos",
            audience="pronaos-gateway",
            jwks_client=_StubJWKSClient(public),  # type: ignore[arg-type]
        )
        now = int(time.time())
        token = jwt.encode(
            {
                "iss": "https://test.idp/realms/pronaos",
                "aud": "pronaos-gateway",
                "iat": now,
                "exp": now + 3600,
                # No "sub" intentionally.
            },
            signing_key_pem,
            algorithm="RS256",
        )
        with pytest.raises(OidcAuthError) as exc:
            verifier.verify(token)
        assert (
            exc.value.reason.startswith("invalid_token") or exc.value.reason == "missing_sub_claim"
        )

    def test_garbage_token_rejected(self, stub_verifier: OidcVerifier) -> None:
        """A token that isn't even a valid JWT shape gets the
        ``jwks_lookup_failed`` reason because PyJWT can't even pull
        the kid out of the header."""
        with pytest.raises(OidcAuthError):
            stub_verifier.verify("not-a-jwt-at-all")


# --------------------------------------------------------------------------- #
# Audience optional                                                           #
# --------------------------------------------------------------------------- #


def test_no_audience_pin_accepts_any_audience(
    rsa_keypair: tuple[Any, Any], signing_key_pem: bytes
) -> None:
    """When ``audience`` is None on the verifier, the JWT's ``aud``
    claim isn't checked. Operator-controlled relaxation for IdPs
    where the audience varies per consumer."""
    _, public = rsa_keypair
    verifier = OidcVerifier(
        issuer="https://test.idp/realms/pronaos",
        audience=None,
        jwks_client=_StubJWKSClient(public),  # type: ignore[arg-type]
    )
    # Mint with an audience the verifier wasn't told about.
    token = _mint_token(signing_key_pem, audience="something-completely-different")
    claims = verifier.verify(token)
    assert claims.audience == "something-completely-different"
