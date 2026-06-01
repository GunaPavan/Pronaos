"""End-to-end OIDC live verification (Phase 26).

Stages the full real-world flow:

1. Generate an RSA-2048 keypair in-process.
2. Serve a static JWKS document over HTTP on port 9101 (so PyJWKClient
   fetches it the same way it would fetch from Keycloak/Auth0/etc.).
3. Mint a JWT signed with the private key.
4. Hit the running Pronaos gateway's ``/v1/admin/usage`` endpoint with
   the JWT in the Bearer header.
5. Show the 200 response — proof that the dual-auth path goes
   token → JWKS fetch → signature verify → tenant resolution → admin
   scope granted.

This script is the live counterpart to the unit tests. The unit tests
stub the JWKS client to skip the network; this script doesn't.

Prerequisites
-------------
The gateway must be running with both env vars set::

    PRONAOS_OIDC_ISSUER=http://localhost:9101
    PRONAOS_OIDC_AUDIENCE=pronaos-gateway
    PRONAOS_OIDC_JWKS_URL=http://localhost:9101/jwks.json

A tenant in the gateway DB must have ``oidc_subject`` set to the
``--subject`` flag value (default: alice@example.com).
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import Any

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


def _b64url_uint(n: int) -> str:
    """JWKS encodes RSA components as base64url-encoded big-endian
    unsigned integers with no padding. PyJWT / Keycloak / Auth0 all
    follow this spec (RFC 7518 §6.3)."""
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _build_jwks(public_key: Any, *, kid: str) -> dict[str, Any]:
    """Construct a one-key JWKS doc from an RSA public key."""
    numbers = public_key.public_numbers()
    return {
        "keys": [
            {
                "kty": "RSA",
                "use": "sig",
                "alg": "RS256",
                "kid": kid,
                "n": _b64url_uint(numbers.n),
                "e": _b64url_uint(numbers.e),
            }
        ]
    }


def _start_jwks_server(jwks: dict[str, Any], *, port: int) -> ThreadingHTTPServer:
    """Stand up a thread-backed HTTP server that serves the JWKS at
    ``/jwks.json``. Runs in the background until the script exits.

    A real OIDC IdP serves at a vendor-specific path (Keycloak:
    ``/realms/{realm}/protocol/openid-connect/certs``; Auth0:
    ``/.well-known/jwks.json``). The gateway's
    ``PRONAOS_OIDC_JWKS_URL`` env var lets operators point at any
    URL, so we use the simpler ``/jwks.json`` here.
    """
    body = json.dumps(jwks).encode("utf-8")

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path != "/jwks.json":
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: Any) -> None:
            # Suppress the noisy per-request stderr log.
            return

    server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _mint_token(
    private_key_pem: bytes,
    *,
    issuer: str,
    audience: str,
    subject: str,
    kid: str,
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
            "preferred_username": subject,
        },
        private_key_pem,
        algorithm="RS256",
        headers={"kid": kid},
    )


async def _hit_gateway(*, base_url: str, token: str) -> tuple[int, str]:
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            f"{base_url}/v1/admin/usage",
            headers={"Authorization": f"Bearer {token}"},
        )
    return resp.status_code, resp.text[:400]


async def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--gateway-url",
        default="http://127.0.0.1:8123",
        help="Pronaos gateway base URL (must be running with OIDC env vars).",
    )
    parser.add_argument(
        "--jwks-port",
        type=int,
        default=9101,
        help="Local port to serve the JWKS on (must match PRONAOS_OIDC_JWKS_URL).",
    )
    parser.add_argument(
        "--issuer",
        default="http://localhost:9101",
        help="Issuer the gateway is configured to accept.",
    )
    parser.add_argument(
        "--audience",
        default="pronaos-gateway",
        help="Audience the gateway is configured to accept.",
    )
    parser.add_argument(
        "--subject",
        default="alice@example.com",
        help="``sub`` claim. Must match a tenant's ``oidc_subject`` column.",
    )
    args = parser.parse_args()

    # 1. Generate keypair.
    print("step 1: generating RSA-2048 keypair")
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = private.public_key()
    private_pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    kid = "pronaos-oidc-demo-key"

    # 2. Serve the JWKS over real HTTP.
    print(f"step 2: serving JWKS at http://127.0.0.1:{args.jwks_port}/jwks.json")
    jwks = _build_jwks(public, kid=kid)
    server = _start_jwks_server(jwks, port=args.jwks_port)

    try:
        # Sanity: fetch our own JWKS to prove the server is reachable.
        async with httpx.AsyncClient(timeout=5.0) as client:
            self_resp = await client.get(f"http://127.0.0.1:{args.jwks_port}/jwks.json")
        if self_resp.status_code != 200:
            print(
                f"  JWKS self-fetch failed: {self_resp.status_code}",
                file=sys.stderr,
            )
            return 2
        print(f"  JWKS self-fetch OK ({len(self_resp.content)} bytes)")

        # 3. Mint a token.
        print(f"step 3: minting JWT with sub={args.subject!r}")
        token = _mint_token(
            private_pem,
            issuer=args.issuer,
            audience=args.audience,
            subject=args.subject,
            kid=kid,
        )
        print(f"  token length: {len(token)} chars, kid={kid}")

        # 4. Hit the gateway.
        print(f"step 4: GET {args.gateway_url}/v1/admin/usage")
        status, body = await _hit_gateway(base_url=args.gateway_url, token=token)
        print(f"  HTTP {status}")
        print(f"  body: {body}")

        if status == 200:
            print()
            print("=" * 64)
            print("✅ VERDICT: claim holds — OIDC dual-auth works end-to-end.")
            print(
                "    JWT → gateway → JWKS fetch over real HTTP → signature\n"
                "    verify → tenant resolution → admin:usage granted."
            )
            return 0
        if status == 401:
            print()
            print(
                "❌ 401 Unauthorized. Most common causes:\n"
                "    - Gateway not started with PRONAOS_OIDC_* env vars\n"
                "    - Issuer / audience / JWKS URL mismatch\n"
                "    - No tenant has oidc_subject = "
                f"{args.subject!r}"
            )
            return 1
        print(f"❌ unexpected status {status}")
        return 1
    finally:
        server.shutdown()


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
