"""Tiny webhook receiver for live-verifying Pronaos webhook delivery.

Usage:
    python scripts/webhook_receiver.py [--port 9090] [--secret SECRET]

Logs every POST it receives, verifies the HMAC-SHA256 signature if a
shared secret is provided, and prints headers + body to stdout.

Not part of the gateway itself — it's a one-file FastAPI app that
acts as a "Slack/PagerDuty stand-in" for the demo. Run it in a
separate terminal, point a tenant's webhook at it, trigger an event,
watch the POST land.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import time
from typing import Any

import uvicorn
from fastapi import FastAPI, Header, Request


def make_app(secret: str | None) -> FastAPI:
    app = FastAPI()

    @app.post("/webhook")
    async def receive(
        request: Request,
        x_pronaos_event: str | None = Header(default=None),
        x_pronaos_signature: str | None = Header(default=None),
        x_pronaos_delivery: str | None = Header(default=None),
    ) -> dict[str, Any]:
        body_bytes = await request.body()
        ts = time.strftime("%H:%M:%S")

        # Verify signature if a secret was provided.
        sig_status = "skipped (no --secret given to receiver)"
        if secret is not None:
            expected = "sha256=" + hmac.new(
                secret.encode("utf-8"), body_bytes, hashlib.sha256
            ).hexdigest()
            sig_status = (
                "VALID" if expected == x_pronaos_signature else "INVALID"
            )

        try:
            payload = json.loads(body_bytes)
        except json.JSONDecodeError:
            payload = {"_raw": body_bytes.decode("utf-8", errors="replace")}

        # ``flush=True`` because uvicorn buffers stdout when redirected
        # to a file; without it the demo viewer sees nothing until the
        # process exits.
        print(f"\n{'=' * 60}", flush=True)
        print(f"[{ts}] received delivery {x_pronaos_delivery}", flush=True)
        print(f"  event:     {x_pronaos_event}", flush=True)
        print(f"  signature: {x_pronaos_signature}", flush=True)
        print(f"  hmac:      {sig_status}", flush=True)
        print("  body:", flush=True)
        print(
            "    " + json.dumps(payload, indent=4).replace("\n", "\n    "),
            flush=True,
        )
        print("=" * 60, flush=True)

        return {"ok": True, "received": x_pronaos_event}

    return app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=9090)
    parser.add_argument(
        "--secret",
        default=None,
        help="Shared secret used to verify the X-Pronaos-Signature header.",
    )
    args = parser.parse_args()

    print(f"Webhook receiver listening on http://127.0.0.1:{args.port}/webhook")
    print(
        f"HMAC verification: {'enabled' if args.secret else 'disabled (pass --secret to verify)'}"
    )

    uvicorn.run(make_app(args.secret), host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
