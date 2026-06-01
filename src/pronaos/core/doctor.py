"""Operator-facing health doctor (Phase 61).

``pronaos-cli doctor`` runs a battery of gates against the
running configuration + connected backing services + seeded
auth state and emits a structured report. The intent: catch
misconfiguration BEFORE a real chat call exposes it.

Design
------
Gates are pure async functions returning ``DoctorGateResult``.
The runner ``run_doctor`` iterates them in a stable order, never
raises, never short-circuits — every gate runs (even if an
earlier one fails) so the operator sees the full picture.

Gate verdicts
-------------
- ``PASS`` — the gate succeeded
- ``FAIL`` — the gate found a real problem; gateway likely
  cannot serve correctly until this is resolved
- ``WARN`` — the gate found a soft issue worth knowing about,
  but the gateway can still serve
- ``SKIP`` — the gate is gated on a feature flag that's off
  (e.g. semantic cache disabled → no Qdrant probe needed)

Exit-code semantics (when wired through the CLI):

- 0 if no FAIL (WARN + SKIP allowed)
- 1 if any FAIL
- ``--strict`` flips WARN to FAIL severity for CI gating

Live-probe opt-in
-----------------
By default the doctor does NOT spend tokens. The
``probe_providers`` flag enables a 1-token roundtrip against
each configured provider — useful when an operator wants the
"yes, my keys actually work end-to-end" signal. Off by default
to keep ``doctor`` cheap to run frequently.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from pronaos.config import Settings, get_settings
from pronaos.db.models import ApiKey, Team, Tenant
from pronaos.providers.catalog import CATALOG

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


class Verdict(StrEnum):
    PASS = "PASS"  # noqa: S105 — gate verdict, not a password
    FAIL = "FAIL"
    WARN = "WARN"
    SKIP = "SKIP"


@dataclass(frozen=True, slots=True)
class DoctorGateResult:
    """One gate's outcome."""

    name: str
    verdict: Verdict
    detail: str = ""


@dataclass(frozen=True, slots=True)
class DoctorReport:
    """The aggregated result of running all gates."""

    gates: list[DoctorGateResult]

    def has_fail(self) -> bool:
        return any(g.verdict == Verdict.FAIL for g in self.gates)

    def has_warn(self) -> bool:
        return any(g.verdict == Verdict.WARN for g in self.gates)

    def exit_code(self, *, strict: bool = False) -> int:
        """0 on clean run; 1 if any FAIL. ``strict`` promotes
        WARN to FAIL for CI gating."""
        if self.has_fail():
            return 1
        if strict and self.has_warn():
            return 1
        return 0

    def to_dict(self) -> dict[str, object]:
        return {
            "gates": [
                {"name": g.name, "verdict": g.verdict.value, "detail": g.detail} for g in self.gates
            ],
            "summary": {
                "pass": sum(1 for g in self.gates if g.verdict == Verdict.PASS),
                "fail": sum(1 for g in self.gates if g.verdict == Verdict.FAIL),
                "warn": sum(1 for g in self.gates if g.verdict == Verdict.WARN),
                "skip": sum(1 for g in self.gates if g.verdict == Verdict.SKIP),
                "total": len(self.gates),
            },
        }


# --------------------------------------------------------------------------- #
# Gate implementations                                                        #
# --------------------------------------------------------------------------- #


def _ok(name: str, detail: str = "") -> DoctorGateResult:
    return DoctorGateResult(name=name, verdict=Verdict.PASS, detail=detail)


def _fail(name: str, detail: str) -> DoctorGateResult:
    return DoctorGateResult(name=name, verdict=Verdict.FAIL, detail=detail)


def _warn(name: str, detail: str) -> DoctorGateResult:
    return DoctorGateResult(name=name, verdict=Verdict.WARN, detail=detail)


def _skip(name: str, detail: str) -> DoctorGateResult:
    return DoctorGateResult(name=name, verdict=Verdict.SKIP, detail=detail)


# ---- Config gates ---------------------------------------------------------- #


async def gate_secret_key(settings: Settings) -> DoctorGateResult:
    """The secret_key must be set + long enough to be non-trivial.

    A short / default key is a security smell; the gateway uses it
    for HMAC signing on outbound webhooks (Phase 18) + audit-chain
    hashing (Phase 10)."""
    name = "config.secret_key"
    if not settings.secret_key:
        return _fail(name, "PRONAOS_SECRET_KEY is not set")
    if len(settings.secret_key) < 32:
        return _warn(
            name,
            f"PRONAOS_SECRET_KEY is only {len(settings.secret_key)} chars; 32+ recommended",
        )
    return _ok(name, f"set ({len(settings.secret_key)} chars)")


async def gate_database_url(settings: Settings) -> DoctorGateResult:
    name = "config.database_url"
    if not settings.database_url:
        return _fail(name, "PRONAOS_DATABASE_URL is not set")
    # Quick parseability check.
    if "://" not in settings.database_url:
        return _fail(
            name,
            f"database_url {settings.database_url!r} doesn't look like a URL",
        )
    scheme = settings.database_url.split("://", 1)[0]
    return _ok(name, f"scheme={scheme}")


# ---- DB gates -------------------------------------------------------------- #


async def gate_db_connect(settings: Settings) -> DoctorGateResult:
    """Open a connection + run SELECT 1."""
    name = "db.connect"
    try:
        engine = create_async_engine(settings.database_url)
        try:
            async with engine.connect() as conn:
                result = await conn.execute(text("SELECT 1"))
                row = result.scalar()
                if row != 1:
                    return _fail(name, f"SELECT 1 returned {row!r}")
        finally:
            await engine.dispose()
    except Exception as e:
        return _fail(name, f"could not connect: {type(e).__name__}: {e}")
    return _ok(name, "SELECT 1 returned 1")


async def gate_db_migrations(settings: Settings) -> DoctorGateResult:
    """alembic_version table exists + matches the latest head."""
    name = "db.migrations"
    try:
        engine = create_async_engine(settings.database_url)
        try:
            async with engine.connect() as conn:
                # Look up the current revision in alembic_version.
                result = await conn.execute(text("SELECT version_num FROM alembic_version"))
                row = result.scalar_one_or_none()
        finally:
            await engine.dispose()
    except Exception as e:
        return _fail(name, f"alembic_version not readable: {e}")
    if row is None:
        return _fail(
            name,
            "alembic_version table is empty — run `pronaos-cli db upgrade`",
        )
    # Compare against the latest migration file in migrations/versions/.
    # We don't import alembic at module-import time; do it lazily.
    latest = _latest_migration_revision()
    if latest is None:
        return _warn(name, f"current={row}, but couldn't locate latest from disk")
    if row != latest:
        return _fail(
            name,
            f"current={row}, latest on disk={latest} — run `pronaos-cli db upgrade`",
        )
    return _ok(name, f"at head ({row})")


def _latest_migration_revision() -> str | None:
    """Read the highest revision id from migrations/versions/.
    Avoids needing to import alembic config + boot a context."""
    from pathlib import Path

    here = Path(__file__).resolve()
    # src/pronaos/core/doctor.py -> repo root is 3 dirs up.
    versions = here.parents[3] / "migrations" / "versions"
    if not versions.exists():
        return None
    # Each migration filename starts with the timestamp/index.
    # The revision id itself lives inside the file as
    # ``revision = "NNNN"``.
    latest_id: str | None = None
    latest_sort = ""
    for path in versions.glob("*.py"):
        if path.name.startswith("_"):
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("revision ="):
                # ``revision = "0025"``
                _, _, value = stripped.partition("=")
                rev = value.strip().strip("\"'")
                # Use the filename as a tiebreaker — alembic doesn't
                # require strict numeric ordering, but Pronaos's
                # migrations are NNNN_ prefixed so this works.
                if path.name > latest_sort:
                    latest_sort = path.name
                    latest_id = rev
                break
    return latest_id


async def gate_core_tables(settings: Settings) -> DoctorGateResult:
    """The handful of tables the chat path absolutely needs at
    request time. If any are missing, migrations didn't run."""
    name = "db.core_tables"
    required = ["tenants", "teams", "api_keys", "usage_records", "batches"]
    try:
        engine = create_async_engine(settings.database_url)
        try:
            async with engine.connect() as conn:
                missing = []
                for t in required:
                    # SQLite + Postgres both accept this form via
                    # the SQL standard's information_schema, but
                    # SQLite uses sqlite_master. Try the universal
                    # approach: SELECT 1 FROM <table> LIMIT 0.
                    try:
                        # nosec / S608 not applicable: ``t`` comes from a
                        # hardcoded allowlist defined just above, never
                        # from user input.
                        await conn.execute(text(f"SELECT 1 FROM {t} LIMIT 0"))  # noqa: S608  # nosec B608
                    except Exception:
                        missing.append(t)
        finally:
            await engine.dispose()
    except Exception as e:
        return _fail(name, f"engine error: {e}")
    if missing:
        return _fail(name, f"missing tables: {', '.join(missing)}")
    return _ok(name, f"all {len(required)} present")


# ---- Auth-seed gates ------------------------------------------------------- #


async def gate_at_least_one_tenant(settings: Settings) -> DoctorGateResult:
    name = "auth.tenant_count"
    try:
        engine = create_async_engine(settings.database_url)
        try:
            sm = async_sessionmaker(engine, expire_on_commit=False)
            async with sm() as session:
                # Avoid select_from since we may be using legacy
                # imports — just use SQL.
                result = await session.execute(text("SELECT COUNT(*) FROM tenants"))
                count = result.scalar()
        finally:
            await engine.dispose()
    except Exception as e:
        return _fail(name, f"could not count tenants: {e}")
    n = int(count or 0)
    if n == 0:
        return _warn(
            name,
            "no tenants seeded; run `pronaos-cli tenant create` before the first chat call",
        )
    return _ok(name, f"{n} tenant(s)")


async def gate_at_least_one_team(settings: Settings) -> DoctorGateResult:
    name = "auth.team_count"
    try:
        engine = create_async_engine(settings.database_url)
        try:
            sm = async_sessionmaker(engine, expire_on_commit=False)
            async with sm() as session:
                result = await session.execute(text("SELECT COUNT(*) FROM teams"))
                count = result.scalar()
        finally:
            await engine.dispose()
    except Exception as e:
        return _fail(name, f"could not count teams: {e}")
    n = int(count or 0)
    if n == 0:
        return _warn(name, "no teams seeded")
    return _ok(name, f"{n} team(s)")


async def gate_at_least_one_active_key(settings: Settings) -> DoctorGateResult:
    name = "auth.active_keys"
    try:
        engine = create_async_engine(settings.database_url)
        try:
            sm = async_sessionmaker(engine, expire_on_commit=False)
            async with sm() as session:
                result = await session.execute(
                    text("SELECT COUNT(*) FROM api_keys WHERE revoked_at IS NULL")
                )
                count = result.scalar()
        finally:
            await engine.dispose()
    except Exception as e:
        return _fail(name, f"could not count active keys: {e}")
    n = int(count or 0)
    if n == 0:
        return _warn(name, "no active API keys — every chat call will 401")
    return _ok(name, f"{n} active key(s)")


# ---- Redis gate ------------------------------------------------------------ #


async def gate_redis(settings: Settings) -> DoctorGateResult:
    name = "redis.ping"
    if not settings.redis_url:
        return _skip(name, "PRONAOS_REDIS_URL not set; in-memory backends active")
    try:
        # Lazy import; Pronaos's deps include redis.
        from redis.asyncio import Redis

        client = Redis.from_url(settings.redis_url, decode_responses=False)
        try:
            pong = await client.ping()
        finally:
            # ``aclose`` is the modern name (redis-py 5.0.1+); the
            # type stubs still expose ``close``, so the typed
            # access goes through getattr to suppress mypy.
            aclose = getattr(client, "aclose", client.close)
            await aclose()
    except Exception as e:
        return _fail(name, f"could not PING: {type(e).__name__}: {e}")
    if not pong:
        return _fail(name, f"PING returned {pong!r}")
    return _ok(name, "PING returned PONG")


# ---- Qdrant gate ----------------------------------------------------------- #


async def gate_qdrant(settings: Settings) -> DoctorGateResult:
    """Only relevant when semantic cache is enabled."""
    name = "qdrant.reachable"
    if not settings.semantic_cache_enabled:
        return _skip(name, "PRONAOS_SEMANTIC_CACHE_ENABLED is false")
    url = settings.qdrant_url.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{url}/")
    except Exception as e:
        return _fail(name, f"could not reach {url}: {e}")
    if r.status_code >= 500:
        return _fail(name, f"GET / returned {r.status_code}")
    return _ok(name, f"GET / returned {r.status_code}")


# ---- Provider catalog gate ------------------------------------------------- #


async def gate_provider_keys(settings: Settings) -> DoctorGateResult:
    """At least one provider in the catalog must have its
    settings_attr populated. If no provider is configured the
    gateway can't serve any chat call."""
    name = "providers.any_configured"
    configured: list[str] = []
    for key, entry in CATALOG.items():
        attr = entry.settings_attr
        if not attr:
            continue
        val = getattr(settings, attr, None)
        if val:
            configured.append(key)
    if not configured:
        return _fail(
            name,
            "no provider API keys configured — set at least one of "
            "ANTHROPIC_API_KEY / OPENAI_API_KEY / GROQ_API_KEY / etc.",
        )
    return _ok(name, f"{len(configured)} provider(s): {', '.join(configured)}")


async def gate_provider_probe(settings: Settings, *, provider_key: str) -> DoctorGateResult:
    """OPT-IN. Probes one provider by listing models (or equivalent).
    Costs zero tokens — no chat call is made. Returns SKIP if the
    provider isn't configured."""
    name = f"providers.probe.{provider_key}"
    entry = CATALOG.get(provider_key)
    if entry is None:
        return _skip(name, "not in catalog")
    if not entry.settings_attr:
        return _skip(name, "no settings_attr")
    api_key = getattr(settings, entry.settings_attr, None)
    if not api_key:
        return _skip(name, f"{entry.settings_attr} not set")
    # Different providers have different "free" liveness endpoints.
    # OpenAI-compat: GET /v1/models. Anthropic: no free endpoint —
    # the cheapest signal is a HEAD on the messages route, but
    # Anthropic returns 405 on HEAD; we settle for verifying the
    # base URL is reachable.
    url = f"{entry.base_url.rstrip('/')}/v1/models"
    auth_header_name = entry.auth.header_name
    auth_header_value = entry.auth.header_format.format(api_key=api_key)
    headers = {auth_header_name: auth_header_value}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(url, headers=headers)
    except Exception as e:
        return _fail(name, f"GET {url} failed: {e}")
    if r.status_code == 200:
        return _ok(name, "GET /v1/models returned 200")
    if r.status_code in (401, 403):
        return _fail(name, f"GET /v1/models returned {r.status_code} — bad key?")
    # Some providers (Anthropic) don't expose /v1/models. We treat
    # any non-401/403 as a soft pass: the URL is reachable and the
    # auth isn't outright rejected.
    return _warn(
        name,
        f"GET /v1/models returned {r.status_code} — not a hard failure but couldn't confirm",
    )


# ---- Optional feature gates ----------------------------------------------- #


async def gate_oidc(settings: Settings) -> DoctorGateResult:
    name = "oidc.discovery"
    issuer = getattr(settings, "oidc_issuer", None)
    if not issuer:
        return _skip(name, "PRONAOS_OIDC_ISSUER not set")
    discovery_url = issuer.rstrip("/") + "/.well-known/openid-configuration"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(discovery_url)
    except Exception as e:
        return _fail(name, f"GET {discovery_url} failed: {e}")
    if r.status_code != 200:
        return _fail(name, f"GET {discovery_url} returned {r.status_code}")
    # Light-touch shape check.
    try:
        doc = r.json()
    except Exception:
        return _fail(name, "discovery response is not JSON")
    if "jwks_uri" not in doc or "issuer" not in doc:
        return _fail(name, "discovery doc missing required fields")
    return _ok(name, f"discovery OK ({discovery_url})")


async def gate_mcp(settings: Settings) -> DoctorGateResult:
    """When MCP is enabled, the adapter module should be importable
    and the SDK should be installed."""
    name = "mcp.enabled"
    if not settings.mcp_enabled:
        return _skip(name, "PRONAOS_MCP_ENABLED is false")
    try:
        from mcp.server import Server  # noqa: F401
    except ImportError as e:
        return _fail(name, f"mcp SDK not installed: {e}")
    try:
        from pronaos.mcp.server import PronaosMcpServer  # noqa: F401
    except ImportError as e:
        return _fail(name, f"PronaosMcpServer not importable: {e}")
    return _ok(name, "MCP server adapter importable")


async def gate_batches_worker(settings: Settings) -> DoctorGateResult:
    name = "batches.worker"
    if not getattr(settings, "batches_worker_enabled", True):
        return _skip(name, "PRONAOS_BATCHES_WORKER_ENABLED=false")
    try:
        from pronaos.core.batch_worker import BatchWorker  # noqa: F401
    except ImportError as e:
        return _fail(name, f"BatchWorker not importable: {e}")
    return _ok(
        name,
        f"poll_interval={settings.batches_poll_interval_seconds}s",
    )


# --------------------------------------------------------------------------- #
# Runner                                                                      #
# --------------------------------------------------------------------------- #


GateFn = "Callable[[Settings], Awaitable[DoctorGateResult]]"


@dataclass(frozen=True, slots=True)
class _RegisteredGate:
    name: str
    fn: Callable[[Settings], Awaitable[DoctorGateResult]]


def _default_gates() -> list[_RegisteredGate]:
    return [
        _RegisteredGate("config.secret_key", gate_secret_key),
        _RegisteredGate("config.database_url", gate_database_url),
        _RegisteredGate("db.connect", gate_db_connect),
        _RegisteredGate("db.migrations", gate_db_migrations),
        _RegisteredGate("db.core_tables", gate_core_tables),
        _RegisteredGate("auth.tenant_count", gate_at_least_one_tenant),
        _RegisteredGate("auth.team_count", gate_at_least_one_team),
        _RegisteredGate("auth.active_keys", gate_at_least_one_active_key),
        _RegisteredGate("redis.ping", gate_redis),
        _RegisteredGate("qdrant.reachable", gate_qdrant),
        _RegisteredGate("providers.any_configured", gate_provider_keys),
        _RegisteredGate("oidc.discovery", gate_oidc),
        _RegisteredGate("mcp.enabled", gate_mcp),
        _RegisteredGate("batches.worker", gate_batches_worker),
    ]


async def run_doctor(
    settings: Settings | None = None,
    *,
    probe_providers: bool = False,
    gates: list[_RegisteredGate] | None = None,
) -> DoctorReport:
    """Execute every gate, never raise, return aggregated report.

    Each gate runs even if an earlier one failed — we want the full
    picture in one shot, not a stop-at-first-error trace.
    """
    settings = settings or get_settings()
    gates = gates or _default_gates()
    results: list[DoctorGateResult] = []
    for g in gates:
        try:
            results.append(await g.fn(settings))
        except Exception as e:
            # Defense in depth: a gate that itself crashes is still
            # reported as a FAIL with the exception text.
            results.append(
                DoctorGateResult(
                    name=g.name,
                    verdict=Verdict.FAIL,
                    detail=f"gate raised {type(e).__name__}: {e}",
                )
            )

    if probe_providers:
        # Probe every configured provider individually.
        for key, entry in CATALOG.items():
            if not entry.settings_attr:
                continue
            if not getattr(settings, entry.settings_attr, None):
                continue
            try:
                results.append(await gate_provider_probe(settings, provider_key=key))
            except Exception as e:
                results.append(
                    DoctorGateResult(
                        name=f"providers.probe.{key}",
                        verdict=Verdict.FAIL,
                        detail=f"gate raised {type(e).__name__}: {e}",
                    )
                )

    return DoctorReport(gates=results)


__all__ = [
    "DoctorGateResult",
    "DoctorReport",
    "Verdict",
    "gate_provider_probe",
    "run_doctor",
]


# Suppress unused-import warning when type-checking is off — the
# imports keep the module's namespace available for downstream
# callers that might want to register custom gates.
_ = (ApiKey, Team, Tenant, os, field)
