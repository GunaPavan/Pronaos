"""Google Cloud service-account JWT → access-token exchange (Phase 53).

GCP's foundation-model API (Vertex AI) doesn't accept long-lived API
keys the way most providers do. Instead it uses **service-account JWT
bearer flow** — the GCP equivalent of AWS SigV4:

1. Operator creates a service account in their GCP project and grants
   it the ``roles/aiplatform.user`` role.
2. Operator downloads the SA key as a JSON file. The JSON includes
   the private RSA key the gateway uses to sign JWTs.
3. The gateway signs a short-lived JWT claiming the
   ``https://www.googleapis.com/auth/cloud-platform`` scope,
   exchanges it at ``https://oauth2.googleapis.com/token`` for a
   ~1-hour OAuth2 access token, and uses that token as a Bearer on
   every Vertex API call. The token is cached and refreshed
   automatically before expiry.

Why not depend on ``google-auth``?
----------------------------------
The ``google-auth`` library would do this in one line, but it pulls
in ~20MB of dependencies (``google-auth``, ``google-api-core``,
``protobuf``, ``grpcio``, etc.) — overkill for one HTTP request. The
``cryptography`` library is already a transitive dep (botocore needs
it for SigV4), so RS256 JWT signing is free. Same posture as the
Bedrock adapter: pure-Python on the hot path, no high-level SDK.

The flow is ~100 lines of code; we'd carry the SDK forever for less
clarity.

Thread safety
-------------
Multiple concurrent chat requests can hit Vertex from the same
gateway process. The auth helper guards the refresh with an
``asyncio.Lock`` so a stampede of expired-token detection doesn't
issue N parallel token-exchange requests — only the first one runs;
the rest await it.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

# OAuth2 grant type for JWT-bearer (RFC 7523).
_JWT_BEARER_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:jwt-bearer"

# GCP-wide scope that grants access to Vertex AI. The narrower
# aiplatform-only scope works too but the project-wide scope is the
# documented happy path and Vertex accepts it.
_VERTEX_SCOPE = "https://www.googleapis.com/auth/cloud-platform"

# Refresh the access token this many seconds before its declared
# expiry. 300s = 5 minutes leaves room for clock skew + an in-flight
# request to land before the token actually expires.
_REFRESH_LEEWAY_SECONDS = 300

# The OAuth2 token endpoint. Stamped on every service-account JSON
# under "token_uri"; we use that field as the source of truth (a
# private GCP install could host its own metadata server, in which
# case "token_uri" points there).
_DEFAULT_TOKEN_URI = "https://oauth2.googleapis.com/token"  # noqa: S105 — URL, not a secret


class VertexAuthError(Exception):
    """Raised when the SA JSON is malformed, the JWT cannot be signed,
    or the OAuth2 token exchange fails. Caught and rewrapped as
    :class:`pronaos.providers.base.AuthError` at the adapter boundary
    so it surfaces as a regular provider auth error.
    """


@dataclass(slots=True)
class _ServiceAccountKey:
    """Parsed shape of the GCP-generated SA key JSON.

    Only the fields the JWT exchange actually needs; ignores
    ``project_id``, ``client_id``, ``auth_uri``, etc. (gateway holds
    project + region separately so the same SA can serve multiple
    regions without re-loading).
    """

    client_email: str
    private_key_pem: bytes
    token_uri: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> _ServiceAccountKey:
        sa_type = data.get("type")
        if sa_type != "service_account":
            raise VertexAuthError(
                f"service-account JSON has type={sa_type!r}; "
                f"expected 'service_account'. Did you download the "
                f"wrong file from the GCP console?"
            )
        client_email = data.get("client_email")
        private_key = data.get("private_key")
        if not isinstance(client_email, str) or not client_email:
            raise VertexAuthError("service-account JSON missing 'client_email' field")
        if not isinstance(private_key, str) or "BEGIN" not in private_key:
            raise VertexAuthError("service-account JSON missing or malformed 'private_key' field")
        token_uri = data.get("token_uri") or _DEFAULT_TOKEN_URI
        return cls(
            client_email=client_email,
            private_key_pem=private_key.encode("utf-8"),
            token_uri=token_uri,
        )


def _b64url_no_padding(raw: bytes) -> str:
    """JWT base64url encoding strips the trailing ``=`` padding bytes
    per RFC 7515 §2."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _sign_assertion(
    key: _ServiceAccountKey,
    *,
    now_unix: int,
    scope: str = _VERTEX_SCOPE,
    ttl_seconds: int = 3600,
) -> str:
    """Build + sign an RS256 JWT bearer assertion for the GCP token
    endpoint.

    The header is the canonical ``{"alg":"RS256","typ":"JWT"}`` —
    Google's token endpoint doesn't require ``kid`` and including
    one is optional. The claims are the minimum the spec mandates:
    ``iss`` (the SA email), ``scope`` (cloud-platform), ``aud`` (the
    token URI), ``iat`` (now), ``exp`` (now+ttl).

    Returns the encoded JWT (header.payload.signature). Pure
    function — no I/O, safe to call from a sync context for tests.
    """
    header = {"alg": "RS256", "typ": "JWT"}
    claims = {
        "iss": key.client_email,
        "scope": scope,
        "aud": key.token_uri,
        "iat": now_unix,
        "exp": now_unix + ttl_seconds,
    }
    header_b64 = _b64url_no_padding(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    claims_b64 = _b64url_no_padding(json.dumps(claims, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{header_b64}.{claims_b64}".encode()

    try:
        private_key = serialization.load_pem_private_key(key.private_key_pem, password=None)
    except Exception as e:
        raise VertexAuthError(f"failed to parse SA private key: {e}") from e
    if not isinstance(private_key, rsa.RSAPrivateKey):
        raise VertexAuthError(
            "SA private key is not RSA — GCP service-account keys are "
            "always RSA-2048 today; got "
            f"{type(private_key).__name__}"
        )

    signature = private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    sig_b64 = _b64url_no_padding(signature)
    return f"{header_b64}.{claims_b64}.{sig_b64}"


@dataclass(slots=True)
class _CachedToken:
    """One cached access token + the wall-clock instant it expires
    (rounded down — never trust a token within the leeway window)."""

    access_token: str
    expires_at_unix: int


class VertexAuth:
    """Async helper that owns the lifecycle of a GCP service-account
    access token.

    Construct once at provider-build time; share across all chat
    completions targeting Vertex. ``access_token()`` is cheap on
    cache hit (no lock contention) and serialised on cache miss so
    only one token exchange runs per refresh window.
    """

    def __init__(
        self,
        *,
        service_account: _ServiceAccountKey,
        http_client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 10.0,
        now_fn: Any = None,
    ) -> None:
        """``now_fn`` is injectable so tests can pin the clock without
        monkey-patching ``time.time``. Defaults to ``time.time``."""
        self._sa = service_account
        self._owns_client = http_client is None
        self._http = http_client or httpx.AsyncClient(timeout=timeout_seconds)
        self._cached: _CachedToken | None = None
        self._lock = asyncio.Lock()
        self._now = now_fn or time.time

    @classmethod
    def from_json_string(cls, json_string: str, **kwargs: Any) -> VertexAuth:
        try:
            data = json.loads(json_string)
        except json.JSONDecodeError as e:
            raise VertexAuthError(f"VERTEX_SERVICE_ACCOUNT_JSON is not valid JSON: {e}") from e
        if not isinstance(data, dict):
            raise VertexAuthError("VERTEX_SERVICE_ACCOUNT_JSON top-level value must be an object")
        return cls(service_account=_ServiceAccountKey.from_dict(data), **kwargs)

    @classmethod
    def from_json_path(cls, path: str | Path, **kwargs: Any) -> VertexAuth:
        try:
            text = Path(path).expanduser().read_text(encoding="utf-8")
        except OSError as e:
            raise VertexAuthError(f"cannot read SA JSON at {path!r}: {e}") from e
        return cls.from_json_string(text, **kwargs)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._http.aclose()

    async def access_token(self) -> str:
        """Return a currently-valid access token. Triggers a token
        exchange iff the cached token is missing or within the
        refresh-leeway window of expiry."""
        now = int(self._now())
        cached = self._cached
        if cached is not None and cached.expires_at_unix - _REFRESH_LEEWAY_SECONDS > now:
            return cached.access_token

        async with self._lock:
            # Re-check under the lock — another task may have refreshed
            # while we were waiting.
            cached = self._cached
            now = int(self._now())
            if cached is not None and cached.expires_at_unix - _REFRESH_LEEWAY_SECONDS > now:
                return cached.access_token

            assertion = _sign_assertion(self._sa, now_unix=now)
            resp = await self._http.post(
                self._sa.token_uri,
                data={
                    "grant_type": _JWT_BEARER_GRANT_TYPE,
                    "assertion": assertion,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            if resp.status_code >= 400:
                # Surface the OAuth2 error body verbatim — it's short
                # and operator-actionable (typically "invalid_grant"
                # with a clock-skew message).
                detail = resp.text[:500]
                raise VertexAuthError(f"GCP token exchange failed: {resp.status_code} {detail}")
            try:
                payload = resp.json()
            except ValueError as e:
                raise VertexAuthError("GCP token exchange returned non-JSON body") from e
            access_token = payload.get("access_token")
            expires_in = payload.get("expires_in")
            if not isinstance(access_token, str) or not access_token:
                raise VertexAuthError("GCP token exchange response missing 'access_token'")
            if not isinstance(expires_in, int) or expires_in <= 0:
                # Default to 1 hour if the response omits expires_in
                # (the spec says it's optional). Conservative — better
                # to refresh slightly too often than to use an expired
                # token.
                expires_in = 3600
            self._cached = _CachedToken(
                access_token=access_token,
                expires_at_unix=now + expires_in,
            )
            return access_token

    async def authorization_header(self) -> dict[str, str]:
        """Convenience wrapper — produces the ``Authorization`` header
        dict for use with httpx. Refreshes the token if needed."""
        token = await self._access_token_text()
        return {"Authorization": f"Bearer {token}"}

    async def _access_token_text(self) -> str:
        return await self.access_token()


# --------------------------------------------------------------------------- #
# Re-exports                                                                  #
# --------------------------------------------------------------------------- #

# The dataclass + signing helper are exposed for tests + the live
# verify script that needs to synthesize JWTs / inspect cached state
# without going through the full HTTP exchange.
__all__ = [
    "VertexAuth",
    "VertexAuthError",
    "_CachedToken",
    "_ServiceAccountKey",
    "_sign_assertion",
]
