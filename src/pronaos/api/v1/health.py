"""Liveness and readiness probes."""

from __future__ import annotations

from fastapi import APIRouter

from pronaos import __version__

router = APIRouter(tags=["system"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness: the process is up."""
    return {"status": "ok", "version": __version__}


@router.get("/readyz")
async def readyz() -> dict[str, str]:
    """Readiness: dependencies (DB, cache, provider catalogue) are reachable.

    Stub for now — expanded in phase 2 when Postgres and Redis wire up.
    """
    return {"status": "ready"}
