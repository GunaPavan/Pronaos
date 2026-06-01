"""Unit tests for the GCP service-account JWT auth helper (Phase 53).

Covers four surfaces:

1. Parsing the SA JSON shape (rejects wrong type / missing fields).
2. RS256 JWT signing (verifies against the public key derived from the
   private key — round-trip without trusting any pre-computed signature).
3. OAuth2 token exchange via respx (claim shape on the wire matches
   RFC 7523 + GCP's documented format).
4. Token caching + refresh under the leeway window.
"""

from __future__ import annotations

import base64
import json
import time

import httpx
import pytest
import respx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from pronaos.providers.vertex_auth import (
    VertexAuth,
    VertexAuthError,
    _ServiceAccountKey,
    _sign_assertion,
)


# Generated once per test session — RSA-2048 takes ~100ms.
@pytest.fixture(scope="module")
def rsa_keypair() -> tuple[rsa.RSAPrivateKey, bytes]:
    """Return ``(private_key, pem_bytes)`` for an ephemeral RSA-2048 key."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return key, pem


@pytest.fixture
def sa_key(rsa_keypair: tuple[rsa.RSAPrivateKey, bytes]) -> _ServiceAccountKey:
    _, pem = rsa_keypair
    return _ServiceAccountKey(
        client_email="vertex-sa@my-project.iam.gserviceaccount.com",
        private_key_pem=pem,
        token_uri="https://oauth2.googleapis.com/token",
    )


def _b64url_decode(s: str) -> bytes:
    """Decode JWT base64url (no padding) bytes."""
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


class TestParseServiceAccountJson:
    def test_accepts_valid_sa_json(self, rsa_keypair: tuple[rsa.RSAPrivateKey, bytes]) -> None:
        _, pem = rsa_keypair
        data = {
            "type": "service_account",
            "client_email": "x@y.iam.gserviceaccount.com",
            "private_key": pem.decode("utf-8"),
            "token_uri": "https://oauth2.googleapis.com/token",
        }
        sa = _ServiceAccountKey.from_dict(data)
        assert sa.client_email == "x@y.iam.gserviceaccount.com"
        assert sa.token_uri == "https://oauth2.googleapis.com/token"
        assert b"BEGIN" in sa.private_key_pem

    def test_rejects_wrong_type(self, rsa_keypair: tuple[rsa.RSAPrivateKey, bytes]) -> None:
        _, pem = rsa_keypair
        data = {
            "type": "user",  # wrong
            "client_email": "x@y.iam.gserviceaccount.com",
            "private_key": pem.decode("utf-8"),
        }
        with pytest.raises(VertexAuthError, match="service_account"):
            _ServiceAccountKey.from_dict(data)

    def test_rejects_missing_client_email(
        self, rsa_keypair: tuple[rsa.RSAPrivateKey, bytes]
    ) -> None:
        _, pem = rsa_keypair
        data = {
            "type": "service_account",
            "private_key": pem.decode("utf-8"),
        }
        with pytest.raises(VertexAuthError, match="client_email"):
            _ServiceAccountKey.from_dict(data)

    def test_rejects_missing_private_key(self) -> None:
        data = {
            "type": "service_account",
            "client_email": "x@y.iam.gserviceaccount.com",
        }
        with pytest.raises(VertexAuthError, match="private_key"):
            _ServiceAccountKey.from_dict(data)

    def test_default_token_uri_when_missing(
        self, rsa_keypair: tuple[rsa.RSAPrivateKey, bytes]
    ) -> None:
        _, pem = rsa_keypair
        data = {
            "type": "service_account",
            "client_email": "x@y.iam.gserviceaccount.com",
            "private_key": pem.decode("utf-8"),
        }
        sa = _ServiceAccountKey.from_dict(data)
        assert sa.token_uri == "https://oauth2.googleapis.com/token"


class TestJwtSigning:
    def test_jwt_shape_is_three_dot_separated(self, sa_key: _ServiceAccountKey) -> None:
        jwt = _sign_assertion(sa_key, now_unix=1_700_000_000)
        assert jwt.count(".") == 2

    def test_header_decodes_to_canonical_rs256(self, sa_key: _ServiceAccountKey) -> None:
        jwt = _sign_assertion(sa_key, now_unix=1_700_000_000)
        parts = jwt.split(".")
        header = json.loads(_b64url_decode(parts[0]))
        assert header == {"alg": "RS256", "typ": "JWT"}

    def test_claims_contain_required_fields(self, sa_key: _ServiceAccountKey) -> None:
        now = 1_700_000_000
        jwt = _sign_assertion(sa_key, now_unix=now, ttl_seconds=3600)
        parts = jwt.split(".")
        claims = json.loads(_b64url_decode(parts[1]))
        assert claims["iss"] == sa_key.client_email
        assert claims["scope"] == "https://www.googleapis.com/auth/cloud-platform"
        assert claims["aud"] == sa_key.token_uri
        assert claims["iat"] == now
        assert claims["exp"] == now + 3600

    def test_signature_verifies_against_public_key(
        self,
        sa_key: _ServiceAccountKey,
        rsa_keypair: tuple[rsa.RSAPrivateKey, bytes],
    ) -> None:
        """Round-trip: sign with private, verify with public. Catches
        any signing-input encoding bug end-to-end."""
        priv, _ = rsa_keypair
        jwt = _sign_assertion(sa_key, now_unix=1_700_000_000)
        parts = jwt.split(".")
        signing_input = f"{parts[0]}.{parts[1]}".encode()
        signature = _b64url_decode(parts[2])
        # If signing was wrong, this verify() raises.
        priv.public_key().verify(signature, signing_input, padding.PKCS1v15(), hashes.SHA256())

    def test_sign_rejects_non_rsa_key(self) -> None:
        """A future GCP rotation that switched to EC keys would not
        crash silently — we raise loudly so operators know to update."""
        from cryptography.hazmat.primitives.asymmetric import ec

        ec_key = ec.generate_private_key(ec.SECP256R1())
        ec_pem = ec_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        sa = _ServiceAccountKey(
            client_email="x@y.iam.gserviceaccount.com",
            private_key_pem=ec_pem,
            token_uri="https://oauth2.googleapis.com/token",
        )
        with pytest.raises(VertexAuthError, match="not RSA"):
            _sign_assertion(sa, now_unix=1_700_000_000)


class TestTokenExchange:
    @pytest.mark.asyncio
    async def test_exchanges_jwt_for_access_token(
        self,
        sa_key: _ServiceAccountKey,
        rsa_keypair: tuple[rsa.RSAPrivateKey, bytes],
    ) -> None:
        """The auth helper POSTs the JWT to the token endpoint and
        returns the access_token from the response."""
        priv, _ = rsa_keypair
        with respx.mock(assert_all_called=True) as mock:
            captured_jwt: list[str] = []

            def _capture(request: httpx.Request) -> httpx.Response:
                # The grant_type + assertion arrive as form-encoded body.
                form = httpx.QueryParams(request.content.decode("utf-8"))
                assert form.get("grant_type") == ("urn:ietf:params:oauth:grant-type:jwt-bearer")
                assertion = form.get("assertion") or ""
                captured_jwt.append(assertion)
                return httpx.Response(
                    200,
                    json={
                        "access_token": "ya29.fake-token-for-test",
                        "expires_in": 3599,
                        "token_type": "Bearer",
                    },
                )

            mock.post("https://oauth2.googleapis.com/token").mock(side_effect=_capture)

            auth = VertexAuth(service_account=sa_key, now_fn=lambda: 1_700_000_000)
            try:
                token = await auth.access_token()
            finally:
                await auth.aclose()

        assert token == "ya29.fake-token-for-test"
        # The JWT in the form body must verify against the SA public key.
        jwt = captured_jwt[0]
        parts = jwt.split(".")
        signing_input = f"{parts[0]}.{parts[1]}".encode()
        signature = _b64url_decode(parts[2])
        priv.public_key().verify(signature, signing_input, padding.PKCS1v15(), hashes.SHA256())

    @pytest.mark.asyncio
    async def test_caches_token_within_validity(self, sa_key: _ServiceAccountKey) -> None:
        """Two calls within the token's validity → one HTTP exchange."""
        call_count = 0

        def _handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return httpx.Response(
                200,
                json={"access_token": f"tok-{call_count}", "expires_in": 3600},
            )

        with respx.mock(assert_all_called=False) as mock:
            mock.post("https://oauth2.googleapis.com/token").mock(side_effect=_handler)
            auth = VertexAuth(service_account=sa_key, now_fn=lambda: 1_700_000_000)
            try:
                t1 = await auth.access_token()
                t2 = await auth.access_token()
            finally:
                await auth.aclose()
        # Same cached token, one upstream call only.
        assert t1 == t2 == "tok-1"
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_refreshes_after_leeway_window(self, sa_key: _ServiceAccountKey) -> None:
        """When the clock advances past ``exp - leeway``, the next
        access_token() call refreshes."""
        call_count = 0

        def _handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return httpx.Response(
                200,
                json={"access_token": f"tok-{call_count}", "expires_in": 3600},
            )

        now = [1_700_000_000]

        with respx.mock(assert_all_called=False) as mock:
            mock.post("https://oauth2.googleapis.com/token").mock(side_effect=_handler)
            auth = VertexAuth(service_account=sa_key, now_fn=lambda: now[0])
            try:
                t1 = await auth.access_token()
                # Advance clock past 3600 - 300 leeway = 3300s
                now[0] += 3301
                t2 = await auth.access_token()
            finally:
                await auth.aclose()
        assert t1 == "tok-1"
        assert t2 == "tok-2"
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_oauth_error_raises_loudly(self, sa_key: _ServiceAccountKey) -> None:
        """A 400 with ``invalid_grant`` (typical clock-skew failure)
        becomes a VertexAuthError with the upstream detail attached."""
        with respx.mock(assert_all_called=True) as mock:
            mock.post("https://oauth2.googleapis.com/token").mock(
                return_value=httpx.Response(
                    400,
                    json={
                        "error": "invalid_grant",
                        "error_description": "JWT has expired or is not valid",
                    },
                )
            )
            auth = VertexAuth(service_account=sa_key)
            try:
                with pytest.raises(VertexAuthError, match="invalid_grant"):
                    await auth.access_token()
            finally:
                await auth.aclose()

    @pytest.mark.asyncio
    async def test_authorization_header_format(self, sa_key: _ServiceAccountKey) -> None:
        with respx.mock(assert_all_called=True) as mock:
            mock.post("https://oauth2.googleapis.com/token").mock(
                return_value=httpx.Response(
                    200,
                    json={"access_token": "ya29.tok", "expires_in": 3600},
                )
            )
            auth = VertexAuth(service_account=sa_key, now_fn=lambda: int(time.time()))
            try:
                header = await auth.authorization_header()
            finally:
                await auth.aclose()
        assert header == {"Authorization": "Bearer ya29.tok"}


class TestFromJsonHelpers:
    def test_from_json_string_round_trip(
        self, rsa_keypair: tuple[rsa.RSAPrivateKey, bytes]
    ) -> None:
        _, pem = rsa_keypair
        data = {
            "type": "service_account",
            "client_email": "x@y.iam.gserviceaccount.com",
            "private_key": pem.decode("utf-8"),
            "token_uri": "https://oauth2.googleapis.com/token",
        }
        auth = VertexAuth.from_json_string(json.dumps(data))
        assert auth._sa.client_email == "x@y.iam.gserviceaccount.com"

    def test_from_json_string_rejects_malformed_json(self) -> None:
        with pytest.raises(VertexAuthError, match="not valid JSON"):
            VertexAuth.from_json_string("{ not json")

    def test_from_json_path_reads_file(
        self,
        tmp_path: object,
        rsa_keypair: tuple[rsa.RSAPrivateKey, bytes],
    ) -> None:
        from pathlib import Path

        assert isinstance(tmp_path, Path)
        _, pem = rsa_keypair
        data = {
            "type": "service_account",
            "client_email": "x@y.iam.gserviceaccount.com",
            "private_key": pem.decode("utf-8"),
        }
        sa_path = tmp_path / "sa.json"
        sa_path.write_text(json.dumps(data), encoding="utf-8")
        auth = VertexAuth.from_json_path(sa_path)
        assert auth._sa.client_email == "x@y.iam.gserviceaccount.com"

    def test_from_json_path_missing_raises(self, tmp_path: object) -> None:
        from pathlib import Path

        assert isinstance(tmp_path, Path)
        with pytest.raises(VertexAuthError, match="cannot read SA JSON"):
            VertexAuth.from_json_path(tmp_path / "doesnotexist.json")
