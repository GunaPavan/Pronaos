"""pronaos-cli — admin tool for tenants, teams, and API keys.

Usage examples (after ``make install`` or ``pip install -e .``):

    pronaos-cli db upgrade
    pronaos-cli tenant create acme
    pronaos-cli team create --tenant <tenant-id> engineering
    pronaos-cli key issue --team <team-id> --label "deploy-prod"
    pronaos-cli key revoke <key-id>
    pronaos-cli tenant list

Keys are shown exactly once at issuance. Losing one means issuing a new one.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Coroutine
from datetime import UTC
from pathlib import Path
from typing import Annotated, Any

import typer
from sqlalchemy import select

from pronaos.auth.api_keys import generate_api_key, hash_key
from pronaos.config import get_settings
from pronaos.db.models import ApiKey, Team, Tenant
from pronaos.db.session import create_engine, create_sessionmaker, get_session

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Pronaos admin CLI.",
)
tenant_app = typer.Typer(help="Manage tenants.")
team_app = typer.Typer(help="Manage teams.")
key_app = typer.Typer(help="Manage API keys.")
db_app = typer.Typer(help="Database operations.")
app.add_typer(tenant_app, name="tenant")
app.add_typer(team_app, name="team")
app.add_typer(key_app, name="key")
app.add_typer(db_app, name="db")


def _run[T](coro: Coroutine[Any, Any, T]) -> T:
    try:
        return asyncio.run(coro)
    except KeyboardInterrupt:
        sys.exit(130)


# --------------------------------------------------------------------------- #
# db                                                                          #
# --------------------------------------------------------------------------- #


@db_app.command("upgrade")
def db_upgrade() -> None:
    """Run Alembic migrations up to head."""
    from alembic import command
    from alembic.config import Config as AlembicConfig

    ini = Path(__file__).resolve().parents[2] / "alembic.ini"
    if not ini.exists():
        typer.echo(f"alembic.ini not found at {ini}", err=True)
        raise typer.Exit(code=1)
    cfg = AlembicConfig(str(ini))
    command.upgrade(cfg, "head")
    typer.echo("ok")


# --------------------------------------------------------------------------- #
# tenant                                                                      #
# --------------------------------------------------------------------------- #


@tenant_app.command("create")
def tenant_create(name: str) -> None:
    """Create a tenant by name."""

    async def _do() -> None:
        engine = create_engine(get_settings())
        sm = create_sessionmaker(engine)
        try:
            async with get_session(sm) as session:
                tenant = Tenant(name=name)
                session.add(tenant)
                await session.flush()
                typer.echo(f"{tenant.id}\t{tenant.name}")
        finally:
            await engine.dispose()

    _run(_do())


@tenant_app.command("list")
def tenant_list() -> None:
    """List all tenants."""

    async def _do() -> None:
        engine = create_engine(get_settings())
        sm = create_sessionmaker(engine)
        try:
            async with get_session(sm) as session:
                rows = (await session.execute(select(Tenant))).scalars().all()
                for t in rows:
                    typer.echo(f"{t.id}\t{t.name}\t{t.created_at.isoformat()}")
        finally:
            await engine.dispose()

    _run(_do())


# --------------------------------------------------------------------------- #
# team                                                                        #
# --------------------------------------------------------------------------- #


@team_app.command("create")
def team_create(
    name: str,
    tenant: Annotated[str, typer.Option(..., help="Tenant id.")],
) -> None:
    """Create a team under a tenant."""

    async def _do() -> None:
        engine = create_engine(get_settings())
        sm = create_sessionmaker(engine)
        try:
            async with get_session(sm) as session:
                team = Team(tenant_id=tenant, name=name)
                session.add(team)
                await session.flush()
                typer.echo(f"{team.id}\t{team.name}\t(tenant={tenant})")
        finally:
            await engine.dispose()

    _run(_do())


@team_app.command("list")
def team_list(
    tenant: Annotated[str | None, typer.Option(help="Filter by tenant id.")] = None,
) -> None:
    async def _do() -> None:
        engine = create_engine(get_settings())
        sm = create_sessionmaker(engine)
        try:
            async with get_session(sm) as session:
                stmt = select(Team)
                if tenant:
                    stmt = stmt.where(Team.tenant_id == tenant)
                for t in (await session.execute(stmt)).scalars().all():
                    typer.echo(f"{t.id}\t{t.name}\t(tenant={t.tenant_id})")
        finally:
            await engine.dispose()

    _run(_do())


# --------------------------------------------------------------------------- #
# key                                                                         #
# --------------------------------------------------------------------------- #


@key_app.command("issue")
def key_issue(
    team: Annotated[str, typer.Option(..., help="Team id.")],
    label: Annotated[str, typer.Option(help="Human-friendly label.")] = "",
    scopes: Annotated[str, typer.Option(help="Space-separated scopes.")] = "chat:write",
    env: Annotated[str, typer.Option(help="live | test")] = "live",
) -> None:
    """Issue a new API key. Shown once — save it immediately."""

    async def _do() -> None:
        engine = create_engine(get_settings())
        sm = create_sessionmaker(engine)
        try:
            async with get_session(sm) as session:
                full_key, prefix = generate_api_key(env=env)
                api_key = ApiKey(
                    team_id=team,
                    prefix=prefix,
                    key_hash=hash_key(full_key),
                    scopes=scopes,
                    label=label,
                )
                session.add(api_key)
                await session.flush()
                typer.echo("")
                typer.echo("  ╭─ API key issued ─────────────────────────────────────")
                typer.echo(f"  │  id:     {api_key.id}")
                typer.echo(f"  │  team:   {team}")
                typer.echo(f"  │  label:  {label or '(none)'}")
                typer.echo(f"  │  scopes: {scopes}")
                typer.echo("  │")
                typer.echo(f"  │  key:    {full_key}")
                typer.echo("  │")
                typer.echo("  │  This is the ONLY time the key will be shown.")
                typer.echo("  │  Copy it now — losing it means issuing a new one.")
                typer.echo("  ╰──────────────────────────────────────────────────────")
                typer.echo("")
        finally:
            await engine.dispose()

    _run(_do())


@key_app.command("revoke")
def key_revoke(id: str) -> None:
    """Revoke a key by id (soft — sets revoked_at, preserves audit trail)."""
    from datetime import datetime

    async def _do() -> None:
        engine = create_engine(get_settings())
        sm = create_sessionmaker(engine)
        try:
            async with get_session(sm) as session:
                api_key = await session.get(ApiKey, id)
                if api_key is None:
                    typer.echo(f"key not found: {id}", err=True)
                    raise typer.Exit(code=1)
                if api_key.revoked_at is not None:
                    typer.echo(f"already revoked at {api_key.revoked_at.isoformat()}")
                    return
                api_key.revoked_at = datetime.now(tz=UTC)
                typer.echo(f"revoked {id}")
        finally:
            await engine.dispose()

    _run(_do())


@key_app.command("list")
def key_list(
    team: Annotated[str | None, typer.Option(help="Filter by team id.")] = None,
) -> None:
    async def _do() -> None:
        engine = create_engine(get_settings())
        sm = create_sessionmaker(engine)
        try:
            async with get_session(sm) as session:
                stmt = select(ApiKey)
                if team:
                    stmt = stmt.where(ApiKey.team_id == team)
                for k in (await session.execute(stmt)).scalars().all():
                    status = "revoked" if k.revoked_at else "active"
                    rps = f"rps={k.rps_limit}" if k.rps_limit is not None else "rps=unlimited"
                    typer.echo(f"{k.id}\t{k.prefix}\t{k.label or '(no label)'}\t{status}\t{rps}")
        finally:
            await engine.dispose()

    _run(_do())


@key_app.command("set-rps")
def key_set_rps(
    id: str,
    rps: Annotated[
        int | None,
        typer.Option(help="Requests-per-second limit. Omit and pass --unlimited to clear."),
    ] = None,
    unlimited: Annotated[
        bool, typer.Option("--unlimited", help="Clear the per-key RPS limit.")
    ] = False,
) -> None:
    """Set or clear the per-key requests-per-second cap.

    Examples:
      pronaos-cli key set-rps <id> --rps 10
      pronaos-cli key set-rps <id> --unlimited
    """
    if unlimited and rps is not None:
        typer.echo("error: pass exactly one of --rps or --unlimited", err=True)
        raise typer.Exit(code=2)
    if not unlimited and rps is None:
        typer.echo("error: must pass --rps N or --unlimited", err=True)
        raise typer.Exit(code=2)
    if rps is not None and rps <= 0:
        typer.echo("error: --rps must be > 0 (use --unlimited to remove the cap)", err=True)
        raise typer.Exit(code=2)

    async def _do() -> None:
        engine = create_engine(get_settings())
        sm = create_sessionmaker(engine)
        try:
            async with get_session(sm) as session:
                api_key = await session.get(ApiKey, id)
                if api_key is None:
                    typer.echo(f"key not found: {id}", err=True)
                    raise typer.Exit(code=1)
                api_key.rps_limit = None if unlimited else rps
                rps_display = "unlimited" if api_key.rps_limit is None else api_key.rps_limit
                typer.echo(f"ok\t{id}\trps={rps_display}")
        finally:
            await engine.dispose()

    _run(_do())


# --------------------------------------------------------------------------- #
# team quotas                                                                 #
# --------------------------------------------------------------------------- #


@team_app.command("set-budget")
def team_set_budget(
    id: str,
    tokens: Annotated[
        int | None,
        typer.Option(help="Monthly token budget. Omit and pass --unlimited to clear."),
    ] = None,
    unlimited: Annotated[
        bool, typer.Option("--unlimited", help="Clear the monthly token budget.")
    ] = False,
) -> None:
    """Set or clear the per-team monthly token budget."""
    if unlimited and tokens is not None:
        typer.echo("error: pass exactly one of --tokens or --unlimited", err=True)
        raise typer.Exit(code=2)
    if not unlimited and tokens is None:
        typer.echo("error: must pass --tokens N or --unlimited", err=True)
        raise typer.Exit(code=2)
    if tokens is not None and tokens <= 0:
        typer.echo(
            "error: --tokens must be > 0 (use --unlimited to remove the budget)",
            err=True,
        )
        raise typer.Exit(code=2)

    async def _do() -> None:
        engine = create_engine(get_settings())
        sm = create_sessionmaker(engine)
        try:
            async with get_session(sm) as session:
                team = await session.get(Team, id)
                if team is None:
                    typer.echo(f"team not found: {id}", err=True)
                    raise typer.Exit(code=1)
                team.monthly_token_budget = None if unlimited else tokens
                budget_display = (
                    "unlimited"
                    if team.monthly_token_budget is None
                    else f"{team.monthly_token_budget:,}"
                )
                typer.echo(f"ok\t{id}\tbudget={budget_display}")
        finally:
            await engine.dispose()

    _run(_do())


@team_app.command("usage")
def team_usage(id: str) -> None:
    """Show the team's current-period token consumption and budget."""

    async def _do() -> None:
        engine = create_engine(get_settings())
        sm = create_sessionmaker(engine)
        try:
            async with get_session(sm) as session:
                team = await session.get(Team, id)
                if team is None:
                    typer.echo(f"team not found: {id}", err=True)
                    raise typer.Exit(code=1)
                budget = team.monthly_token_budget
                used = team.current_period_tokens
                resets_at = team.period_resets_at
                if resets_at.tzinfo is None:
                    resets_at = resets_at.replace(tzinfo=UTC)
                budget_display = f"{budget:,}" if budget is not None else "unlimited"
                pct = f" ({100 * used / budget:.1f}%)" if budget else ""
                typer.echo(f"team:    {team.name} ({team.id})")
                typer.echo(f"used:    {used:,} tokens{pct}")
                typer.echo(f"budget:  {budget_display}")
                typer.echo(f"resets:  {resets_at.isoformat()}")
        finally:
            await engine.dispose()

    _run(_do())


def main() -> None:
    app()


if __name__ == "__main__":
    main()
