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
import contextlib
import sys
from collections.abc import Coroutine
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import typer
from sqlalchemy import func, select

from pronaos.auth.api_keys import generate_api_key, hash_key
from pronaos.config import get_settings
from pronaos.db.models import ApiKey, Team, Tenant, UsageRecord
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
eval_app = typer.Typer(help="Run evaluation suites.")
audit_app = typer.Typer(help="Inspect + verify the hash-chained audit log.")
abtest_app = typer.Typer(help="Manage per-team A/B routing tests.")
app.add_typer(tenant_app, name="tenant")
app.add_typer(team_app, name="team")
app.add_typer(key_app, name="key")
app.add_typer(db_app, name="db")
app.add_typer(eval_app, name="eval")
app.add_typer(audit_app, name="audit")
app.add_typer(abtest_app, name="abtest")


def _run[T](coro: Coroutine[Any, Any, T]) -> T:
    try:
        return asyncio.run(coro)
    except KeyboardInterrupt:
        sys.exit(130)


# --------------------------------------------------------------------------- #
# doctor — operator health check (Phase 61)                                   #
# --------------------------------------------------------------------------- #


@app.command("doctor")
def doctor(
    probe_providers: Annotated[
        bool,
        typer.Option(
            "--probe-providers",
            help=(
                "Hit each configured provider with a /v1/models probe "
                "(no tokens spent). Off by default to keep `doctor` cheap "
                "to run frequently."
            ),
        ),
    ] = False,
    strict: Annotated[
        bool,
        typer.Option(
            "--strict",
            help=(
                "Treat WARN as FAIL for exit-code purposes. Useful when "
                "wiring `doctor` into CI as a deploy gate."
            ),
        ),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option(
            "--json",
            help="Print the report as JSON instead of human-readable lines.",
        ),
    ] = False,
) -> None:
    """Run the operator health check (Phase 61).

    Executes ~14 gates against config, DB, Redis, Qdrant, provider
    catalog, auth seeds, and optional features (MCP, OIDC, batches
    worker). Each gate is independent — every gate runs even if an
    earlier one failed. Prints PASS/FAIL/WARN/SKIP per gate plus a
    final verdict line. Exit 0 = no FAILs (WARN/SKIP allowed);
    exit 1 = any FAIL (or any WARN with --strict).

    Examples
    --------
        # Quick health check, no token spend:
        pronaos-cli doctor

        # CI gate:
        pronaos-cli doctor --strict

        # JSON for piping into jq:
        pronaos-cli doctor --json | jq '.summary'

        # Confirm provider keys actually work:
        pronaos-cli doctor --probe-providers
    """
    from pronaos.core.doctor import Verdict, run_doctor

    async def _do() -> int:
        report = await run_doctor(probe_providers=probe_providers)
        if json_output:
            import json as _json

            typer.echo(_json.dumps(report.to_dict(), indent=2))
        else:
            for g in report.gates:
                marker = g.verdict.value
                line = f"[{marker:<4}] {g.name}"
                if g.detail:
                    line += f"  --  {g.detail}"
                typer.echo(line)
            n_pass = sum(1 for g in report.gates if g.verdict == Verdict.PASS)
            n_fail = sum(1 for g in report.gates if g.verdict == Verdict.FAIL)
            n_warn = sum(1 for g in report.gates if g.verdict == Verdict.WARN)
            n_skip = sum(1 for g in report.gates if g.verdict == Verdict.SKIP)
            typer.echo("")
            typer.echo(f"summary: {n_pass} pass / {n_fail} fail / {n_warn} warn / {n_skip} skip")
            if report.has_fail():
                typer.echo("VERDICT: gateway has at least one FAILing gate", err=True)
            elif report.has_warn() and strict:
                typer.echo("VERDICT: --strict in effect; WARN treated as FAIL", err=True)
            else:
                typer.echo("VERDICT: gateway is healthy")
        # Silence unused-import warning when probe is off.
        _ = Verdict
        return report.exit_code(strict=strict)

    sys.exit(_run(_do()))


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


@tenant_app.command("set-webhook")
def tenant_set_webhook(
    id: str,
    url: Annotated[
        str | None,
        typer.Option(
            "--url",
            help=(
                "Webhook URL to POST events to. Must be set together with "
                "--secret. Pass --clear to disable webhooks for this tenant."
            ),
        ),
    ] = None,
    secret: Annotated[
        str | None,
        typer.Option(
            "--secret",
            help=(
                "Shared secret used to HMAC-SHA256-sign every payload. "
                "Receivers verify via the X-Pronaos-Signature header."
            ),
        ),
    ] = None,
    clear: Annotated[
        bool,
        typer.Option(
            "--clear",
            help="Disable webhooks for this tenant (clear both url + secret).",
        ),
    ] = False,
    show: Annotated[
        bool,
        typer.Option(
            "--show",
            help="Print the current webhook config (secret is redacted).",
        ),
    ] = False,
) -> None:
    """Manage per-tenant operational-event webhook delivery.

    Events fired today (Phase 19):

    - ``quota.exhausted``  — fires when a tenant's request is denied
      for token-budget, cost-budget, or rate-limit exhaustion
    - ``circuit.tripped``  — fires when a provider's circuit breaker
      transitions to OPEN
    - ``audit.chain_broken`` — fires from ``pronaos-cli audit verify``
      when chain integrity verification detects tampering

    Every payload is HTTP POSTed with an HMAC-SHA256 signature in
    ``X-Pronaos-Signature: sha256=<hex>`` so receivers can verify
    authenticity without trusting the channel.

    Examples
    --------
        # Configure:
        pronaos-cli tenant set-webhook <id> \\
            --url https://hooks.slack.com/services/X/Y/Z \\
            --secret supersecret

        # Show current state:
        pronaos-cli tenant set-webhook <id> --show

        # Disable:
        pronaos-cli tenant set-webhook <id> --clear
    """

    async def _do() -> None:
        engine = create_engine(get_settings())
        sm = create_sessionmaker(engine)
        try:
            async with get_session(sm) as session:
                tenant_row = await session.get(Tenant, id)
                if tenant_row is None:
                    typer.echo(f"tenant not found: {id}", err=True)
                    raise typer.Exit(code=1)

                if show:
                    if not tenant_row.webhook_url:
                        typer.echo("(webhooks disabled)")
                    else:
                        typer.echo(f"url:    {tenant_row.webhook_url}")
                        # Secret is sensitive — redact, just show that it's set.
                        has_secret = bool(tenant_row.webhook_secret)
                        typer.echo(f"secret: {'(set)' if has_secret else '(missing)'}")
                    return

                if clear:
                    tenant_row.webhook_url = None
                    tenant_row.webhook_secret = None
                    typer.echo(f"ok\t{id}\twebhook=disabled")
                    return

                if url is None or secret is None:
                    typer.echo(
                        "error: pass BOTH --url and --secret (or --clear / --show)",
                        err=True,
                    )
                    raise typer.Exit(code=2)

                if not url.startswith(("http://", "https://")):
                    typer.echo(
                        f"error: webhook URL must start with http(s):// — got {url!r}",
                        err=True,
                    )
                    raise typer.Exit(code=2)

                tenant_row.webhook_url = url
                tenant_row.webhook_secret = secret
                typer.echo(f"ok\t{id}\twebhook={url}")
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


@team_app.command("set-cost-budget")
def team_set_cost_budget(
    id: str,
    cents: Annotated[
        int | None,
        typer.Option(
            help="Monthly cost budget in whole cents (e.g. 5000 = $50.00). "
            "Omit and pass --unlimited to clear."
        ),
    ] = None,
    unlimited: Annotated[
        bool, typer.Option("--unlimited", help="Clear the monthly cost budget.")
    ] = False,
) -> None:
    """Set or clear the per-team monthly cost budget.

    Cents (not hundredths-of-a-cent) for ergonomic CLI input — admins
    set "$50" not "500,000". Stored as hcents internally for sub-cent
    precision matching the provider pricing tables.
    """
    if unlimited and cents is not None:
        typer.echo("error: pass exactly one of --cents or --unlimited", err=True)
        raise typer.Exit(code=2)
    if not unlimited and cents is None:
        typer.echo("error: must pass --cents N or --unlimited", err=True)
        raise typer.Exit(code=2)
    if cents is not None and cents <= 0:
        typer.echo(
            "error: --cents must be > 0 (use --unlimited to remove the budget)",
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
                hcents = None if unlimited else (cents * 100 if cents else None)
                team.monthly_cost_hcents_budget = hcents
                display = "unlimited" if hcents is None else f"${hcents / 10_000:,.2f}"
                typer.echo(f"ok\t{id}\tcost_budget={display}")
        finally:
            await engine.dispose()

    _run(_do())


@team_app.command("usage")
def team_usage(id: str) -> None:
    """Show the team's current-period consumption against both budgets."""

    async def _do() -> None:
        engine = create_engine(get_settings())
        sm = create_sessionmaker(engine)
        try:
            async with get_session(sm) as session:
                team = await session.get(Team, id)
                if team is None:
                    typer.echo(f"team not found: {id}", err=True)
                    raise typer.Exit(code=1)
                token_budget = team.monthly_token_budget
                tokens_used = team.current_period_tokens
                cost_budget_hcents = team.monthly_cost_hcents_budget
                cost_used_hcents = team.current_period_cost_hcents
                resets_at = team.period_resets_at
                if resets_at.tzinfo is None:
                    resets_at = resets_at.replace(tzinfo=UTC)

                tb_display = f"{token_budget:,}" if token_budget is not None else "unlimited"
                tb_pct = f" ({100 * tokens_used / token_budget:.1f}%)" if token_budget else ""
                cb_display = (
                    f"${cost_budget_hcents / 10_000:,.2f}"
                    if cost_budget_hcents is not None
                    else "unlimited"
                )
                cb_pct = (
                    f" ({100 * cost_used_hcents / cost_budget_hcents:.1f}%)"
                    if cost_budget_hcents
                    else ""
                )

                typer.echo(f"team:         {team.name} ({team.id})")
                typer.echo(f"tokens used:  {tokens_used:,}{tb_pct}")
                typer.echo(f"token budget: {tb_display}")
                typer.echo(f"cost used:    ${cost_used_hcents / 10_000:,.4f}{cb_pct}")
                typer.echo(f"cost budget:  {cb_display}")
                typer.echo(f"resets:       {resets_at.isoformat()}")
        finally:
            await engine.dispose()

    _run(_do())


# --------------------------------------------------------------------------- #
# team chargeback                                                             #
# --------------------------------------------------------------------------- #


_VALID_GROUPS = {"provider", "model", "status"}


def _start_of_current_month_utc(now: datetime | None = None) -> datetime:
    """First day of the current calendar month at 00:00 UTC.

    Mirror of ``next_period_reset`` for the *previous* boundary. Used as the
    default ``--since`` for chargeback so "no flags" means "this billing month."
    """
    now = now or datetime.now(tz=UTC)
    return datetime(now.year, now.month, 1, 0, 0, 0, tzinfo=UTC)


def _parse_iso_or_die(label: str, raw: str) -> datetime:
    """Parse an ISO-8601 string; treat bare dates and naive datetimes as UTC."""
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError as e:
        typer.echo(f"error: --{label} {raw!r} is not a valid ISO datetime: {e}", err=True)
        raise typer.Exit(code=2) from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _format_usd(hcents: int) -> str:
    """Render hundredths-of-a-cent as USD with 4-decimal precision.

    1 cent = 100 hcents, $1 = 10_000 hcents. Four decimals lets a $0.0042
    Groq call still render as $0.0042 instead of rounding to $0.00 and
    looking like a free request.
    """
    return f"${hcents / 10_000:,.4f}"


@team_app.command("chargeback")
def team_chargeback(
    id: str,
    since: Annotated[
        str | None,
        typer.Option(help="ISO datetime lower bound (default: start of current month UTC)."),
    ] = None,
    until: Annotated[
        str | None,
        typer.Option(help="ISO datetime upper bound, exclusive (default: now)."),
    ] = None,
    group_by: Annotated[str, typer.Option(help="provider | model | status")] = "model",
) -> None:
    """Per-call chargeback summary for a team.

    Reads ``usage_records`` (Phase 5.1+). Output: an overall totals line
    plus a breakdown by the requested grouping column. Default window is
    the current calendar month — pass ``--since`` / ``--until`` to override.

    Examples::

        pronaos-cli team chargeback <team-id>
        pronaos-cli team chargeback <team-id> --group-by provider
        pronaos-cli team chargeback <team-id> --since 2026-05-01 --group-by model
    """
    if group_by not in _VALID_GROUPS:
        typer.echo(
            f"error: --group-by must be one of {sorted(_VALID_GROUPS)}, got {group_by!r}",
            err=True,
        )
        raise typer.Exit(code=2)

    start_dt = _parse_iso_or_die("since", since) if since else _start_of_current_month_utc()
    end_dt = _parse_iso_or_die("until", until) if until else datetime.now(tz=UTC)
    if end_dt <= start_dt:
        typer.echo("error: --until must be strictly after --since", err=True)
        raise typer.Exit(code=2)

    group_col = {
        "provider": UsageRecord.provider,
        "model": UsageRecord.model,
        "status": UsageRecord.status,
    }[group_by]

    async def _do() -> None:
        engine = create_engine(get_settings())
        sm = create_sessionmaker(engine)
        try:
            async with get_session(sm) as session:
                team = await session.get(Team, id)
                if team is None:
                    typer.echo(f"team not found: {id}", err=True)
                    raise typer.Exit(code=1)

                base_where = [
                    UsageRecord.team_id == id,
                    UsageRecord.ts >= start_dt,
                    UsageRecord.ts < end_dt,
                ]

                # Overall totals
                totals_stmt = select(
                    func.count(UsageRecord.id),
                    func.coalesce(func.sum(UsageRecord.prompt_tokens), 0),
                    func.coalesce(func.sum(UsageRecord.completion_tokens), 0),
                    func.coalesce(func.sum(UsageRecord.cost_hcents), 0),
                ).where(*base_where)
                requests, prompt_sum, completion_sum, cost_sum = (
                    await session.execute(totals_stmt)
                ).one()
                requests = int(requests)
                total_tokens = int(prompt_sum) + int(completion_sum)
                cost_sum = int(cost_sum)

                # Grouped breakdown — ordered by cost descending so the
                # biggest spender bubbles to the top.
                group_stmt = (
                    select(
                        group_col,
                        func.count(UsageRecord.id),
                        func.coalesce(func.sum(UsageRecord.prompt_tokens), 0),
                        func.coalesce(func.sum(UsageRecord.completion_tokens), 0),
                        func.coalesce(func.sum(UsageRecord.cost_hcents), 0),
                    )
                    .where(*base_where)
                    .group_by(group_col)
                    .order_by(func.sum(UsageRecord.cost_hcents).desc())
                )
                groups = (await session.execute(group_stmt)).all()

                # Header
                typer.echo(f"team:     {team.name} ({team.id})")
                typer.echo(f"window:   {start_dt.isoformat()}  →  {end_dt.isoformat()}")
                typer.echo(f"requests: {requests:,}")
                typer.echo(
                    f"tokens:   {total_tokens:,}  "
                    f"({int(prompt_sum):,} in / {int(completion_sum):,} out)"
                )
                typer.echo(f"cost:     {_format_usd(cost_sum)}")

                if not groups:
                    typer.echo("(no usage in window)")
                    return

                typer.echo("")
                typer.echo(f"by {group_by}:")
                # Pretty column widths. The group label can be long
                # ("anthropic/claude-opus-4-7") so size it dynamically.
                name_width = max(len(str(g[0])) for g in groups)
                name_width = max(name_width, len(group_by))
                # header
                typer.echo(
                    f"  {group_by:<{name_width}}  {'requests':>8}  {'tokens':>12}  {'cost':>12}"
                )
                for name, gcount, gprompt, gcompletion, gcost in groups:
                    gtokens = int(gprompt) + int(gcompletion)
                    typer.echo(
                        f"  {name!s:<{name_width}}  {int(gcount):>8,}  "
                        f"{gtokens:>12,}  {_format_usd(int(gcost)):>12}"
                    )
        finally:
            await engine.dispose()

    _run(_do())


# --------------------------------------------------------------------------- #
# eval — LLM-as-judge evaluation suite (Phase 9)                              #
# --------------------------------------------------------------------------- #


@eval_app.command("run")
def eval_run(
    golden_set: Annotated[
        Path,
        typer.Option(
            "--golden-set",
            "-g",
            help="Path to a YAML golden-set file (see tests/eval/data/).",
        ),
    ],
    candidate_model: Annotated[
        str,
        typer.Option(
            "--candidate-model",
            "-c",
            help="Model id to evaluate (e.g. groq/llama-3.1-8b-instant).",
        ),
    ],
    judge_model: Annotated[
        str,
        typer.Option(
            "--judge-model",
            "-j",
            help=(
                "Model id used as the judge (e.g. anthropic/claude-haiku-4-5). "
                "Choose a stronger model than the candidate when you can. "
                "Pass a comma-separated list (e.g. "
                "'groq/llama-3.3-70b-versatile,anthropic/claude-3-7-sonnet') "
                "to run multi-judge eval with inter-judge agreement (Phase 23)."
            ),
        ),
    ],
    api_key: Annotated[
        str,
        typer.Option(
            "--api-key",
            "-k",
            help="Bearer key for the gateway (used for both candidate + judge calls).",
        ),
    ],
    base_url: Annotated[str, typer.Option(help="Gateway base URL.")] = "http://localhost:8080",
    pass_threshold: Annotated[
        float,
        typer.Option(
            "--threshold",
            help="A case 'passes' when its score >= this (0.0-1.0).",
        ),
    ] = 0.7,
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Save the full result JSON to this path."),
    ] = None,
) -> None:
    """Run an LLM-as-judge evaluation against a candidate model.

    Examples
    --------
        # smoke run against the bundled basic golden set:
        pronaos-cli eval run \\
            -g tests/eval/data/basic.yaml \\
            -c groq/llama-3.1-8b-instant \\
            -j anthropic/claude-haiku-4-5 \\
            -k pn_live_...

        # save the result JSON for diffing across runs:
        pronaos-cli eval run ... -o eval-results/$(date +%F).json
    """
    from pronaos.eval.data import load_golden_set
    from pronaos.eval.multi_judge import MultiJudgeRunner
    from pronaos.eval.scorer import LLMJudgeScorer

    try:
        gs = load_golden_set(golden_set)
    except (FileNotFoundError, ValueError) as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(code=2) from None

    # Parse the judge-model argument. Comma-separated -> multi-judge.
    judge_ids = [j.strip() for j in judge_model.split(",") if j.strip()]
    if not judge_ids:
        typer.echo("error: --judge-model must name at least one model", err=True)
        raise typer.Exit(code=2)

    if len(judge_ids) == 1:
        _run_single_judge(
            gs=gs,
            candidate_model=candidate_model,
            judge_model=judge_ids[0],
            api_key=api_key,
            base_url=base_url,
            pass_threshold=pass_threshold,
            output=output,
        )
    else:
        scorers = [
            (
                jid,
                LLMJudgeScorer(base_url=base_url, api_key=api_key, judge_model=jid),
            )
            for jid in judge_ids
        ]
        runner = MultiJudgeRunner(
            candidate_base_url=base_url,
            candidate_api_key=api_key,
            candidate_model=candidate_model,
            scorers=scorers,
            pass_threshold=pass_threshold,
        )

        typer.echo(f"golden set:  {gs.name} ({len(gs)} cases)")
        typer.echo(f"candidate:   {candidate_model}")
        typer.echo(f"judges:      {', '.join(judge_ids)}")
        typer.echo(f"threshold:   {pass_threshold:.2f}")
        typer.echo("")

        mj_summary = _run(runner.run(gs))

        # ---- per-row table with per-judge columns ----
        header = f"{'case':<28} {'cat':<14}  " + "  ".join(f"{jid[:18]:>18}" for jid in judge_ids)
        typer.echo(header)
        typer.echo("-" * len(header))
        for row in mj_summary.rows:
            cols = []
            for jid in judge_ids:
                v = next((v for v in row.verdicts if v.judge_id == jid), None)
                if v is None or not v.is_valid:
                    cols.append(f"{'ERR':>18}")
                else:
                    cols.append(f"{v.score:>18.2f}")
            err = f"  CANDIDATE ERR: {row.candidate_error[:32]}" if row.candidate_error else ""
            typer.echo(f"{row.case_id:<28} {row.category:<14}  " + "  ".join(cols) + err)

        # ---- per-judge stats ----
        typer.echo("")
        typer.echo("=" * 80)
        typer.echo("per-judge stats:")
        typer.echo(f"  {'judge':<40} {'n':>4} {'mean':>6} {'median':>7} {'pass':>6}")
        for s in mj_summary.per_judge:
            typer.echo(
                f"  {s.judge_id:<40} {s.n_scored:>4} {s.mean:>6.2f} "
                f"{s.median:>7.2f} {s.pass_rate:>5.0%}"
            )

        # ---- pairwise agreement ----
        typer.echo("")
        typer.echo("inter-judge agreement:")
        typer.echo(f"  {'pair':<60}  {'n':>3} {'mean Δ':>7} {'≤ε':>5} {'κ':>6}")
        for p in mj_summary.pairs:
            typer.echo(
                f"  {p.judge_a + ' ↔ ' + p.judge_b:<60}  "
                f"{p.n:>3} {p.mean_abs_delta:>7.3f} "
                f"{p.within_epsilon_rate:>4.0%} {p.cohens_kappa:>6.2f}"
            )

        typer.echo("")
        typer.echo(f"duration:          {mj_summary.duration_seconds:.1f}s")

        if output is not None:
            mj_summary.save_json(output)
            typer.echo("")
            typer.echo(f"saved: {output}")


def _run_single_judge(
    *,
    gs: Any,
    candidate_model: str,
    judge_model: str,
    api_key: str,
    base_url: str,
    pass_threshold: float,
    output: Path | None,
) -> None:
    """Original single-judge path. Extracted from eval_run for clarity now
    that the command branches on judge count."""
    from pronaos.eval.runner import EvalRunner
    from pronaos.eval.scorer import LLMJudgeScorer

    scorer = LLMJudgeScorer(base_url=base_url, api_key=api_key, judge_model=judge_model)
    runner = EvalRunner(
        candidate_base_url=base_url,
        candidate_api_key=api_key,
        candidate_model=candidate_model,
        scorer=scorer,
        judge_model_id=judge_model,
        pass_threshold=pass_threshold,
    )

    typer.echo(f"golden set:  {gs.name} ({len(gs)} cases)")
    typer.echo(f"candidate:   {candidate_model}")
    typer.echo(f"judge:       {judge_model}")
    typer.echo(f"threshold:   {pass_threshold:.2f}")
    typer.echo("")

    summary = _run(runner.run(gs))

    # ---- per-row table ----
    typer.echo(f"{'case':<28} {'cat':<14} {'score':>6}  reason")
    typer.echo("-" * 80)
    for row in summary.rows:
        if row.candidate_error:
            typer.echo(
                f"{row.case_id:<28} {row.category:<14} {'ERR':>6}  {row.candidate_error[:36]}"
            )
        elif row.judge_error:
            typer.echo(
                f"{row.case_id:<28} {row.category:<14} {'judge?':>6}  {row.judge_error[:36]}"
            )
        else:
            why = row.justification[:42] if row.justification else ""
            typer.echo(f"{row.case_id:<28} {row.category:<14} {row.score:>6.2f}  {why}")

    # ---- aggregate ----
    typer.echo("")
    typer.echo("=" * 80)
    typer.echo(f"total cases:       {summary.total_cases}")
    typer.echo(f"scored:            {summary.scored_cases}")
    typer.echo(f"candidate errors:  {summary.candidate_errors}")
    typer.echo(f"judge errors:      {summary.judge_errors}")
    typer.echo(f"overall mean:      {summary.overall_mean:.3f}")
    typer.echo(f"overall median:    {summary.overall_median:.3f}")
    typer.echo(f"pass rate (≥{pass_threshold:.2f}): {summary.overall_pass_rate:.1%}")
    typer.echo(f"duration:          {summary.duration_seconds:.1f}s")

    if summary.categories:
        typer.echo("")
        typer.echo("by category:")
        typer.echo(f"  {'category':<20} {'n':>4} {'mean':>6} {'pass':>6}")
        for c in summary.categories:
            typer.echo(f"  {c.name:<20} {c.count:>4} {c.mean:>6.2f} {c.pass_rate:>5.0%}")

    if output is not None:
        summary.save_json(output)
        typer.echo("")
        typer.echo(f"saved: {output}")


# --------------------------------------------------------------------------- #
# eval store-scores (Phase 24)                                                #
# --------------------------------------------------------------------------- #


@eval_app.command("store-scores")
def eval_store_scores(
    team_id: Annotated[
        str,
        typer.Option(
            "--team",
            "-t",
            help="Team id whose quality_scores column will be updated.",
        ),
    ],
    from_path: Annotated[
        Path,
        typer.Option(
            "--from",
            "-f",
            help=(
                "Path to an eval result JSON saved by ``eval run -o <path>``. "
                "Either single-judge (EvalRunSummary) or multi-judge "
                "(MultiJudgeEvalSummary) shape is accepted; multi-judge "
                "scores are averaged across judges per model."
            ),
        ),
    ],
    show: Annotated[
        bool,
        typer.Option(
            "--show",
            help="Print the team's current stored quality scores and exit.",
        ),
    ] = False,
    clear: Annotated[
        bool,
        typer.Option(
            "--clear",
            help=("Clear the team's quality_scores column (back to no eval data on record)."),
        ),
    ] = False,
) -> None:
    """Persist per-model quality scores for the quality-aware router.

    The router uses ``team.quality_scores`` plus ``team.quality_threshold``
    when ``routing_strategy="quality-aware-cheapest"``. This command is
    the bridge between the eval harness (Phase 9 / Phase 23 multi-judge)
    and the cost-aware router (Phase 21).

    Workflow
    --------
    1. Run an eval against candidate models, saving JSON output::

        pronaos-cli eval run -g tests/eval/data/basic.yaml \\
            -c groq/llama-3.1-8b-instant -j groq/llama-3.3-70b-versatile \\
            -k <api-key> -o eval-results/run.json

       (Or a multi-judge run with two -j models; both shapes work.)

    2. Persist the scores onto the team::

        pronaos-cli eval store-scores --team <team-id> \\
            --from eval-results/run.json

    3. Switch the team to quality-aware routing::

        pronaos-cli team set-routing-strategy <team-id> \\
            --strategy quality-aware-cheapest

    The candidate model from the eval is stored under its fqmn key in
    ``team.quality_scores``. Running this command again with a *different*
    candidate adds a new entry; running with the same candidate replaces
    the prior entry (latest score wins).
    """
    import json
    from datetime import UTC, datetime

    async def _do() -> None:
        engine = create_engine(get_settings())
        sm = create_sessionmaker(engine)
        try:
            async with get_session(sm) as session:
                team = await session.get(Team, team_id)
                if team is None:
                    typer.echo(f"team not found: {team_id}", err=True)
                    raise typer.Exit(code=1)

                if show:
                    if not team.quality_scores:
                        typer.echo("(no quality scores stored)")
                    else:
                        typer.echo(json.dumps(team.quality_scores, indent=2, sort_keys=True))
                    return

                if clear:
                    team.quality_scores = None
                    typer.echo(f"ok\t{team_id}\tquality_scores=cleared")
                    return

                if not from_path.exists():
                    typer.echo(f"file not found: {from_path}", err=True)
                    raise typer.Exit(code=2)

                try:
                    raw = json.loads(from_path.read_text(encoding="utf-8"))
                except json.JSONDecodeError as e:
                    typer.echo(f"invalid JSON in {from_path}: {e}", err=True)
                    raise typer.Exit(code=2) from None

                fqmn, score, n_samples = _parse_eval_summary(raw)
                if fqmn is None or score is None:
                    typer.echo(
                        "unrecognised eval result shape — expected "
                        "EvalRunSummary or MultiJudgeEvalSummary JSON",
                        err=True,
                    )
                    raise typer.Exit(code=2)

                # Merge: keep existing scores for other models, overwrite
                # the one we just learned about.
                current: dict[str, Any] = dict(team.quality_scores or {})
                current[fqmn] = {
                    "score": float(score),
                    "n_samples": int(n_samples),
                    "source_eval_id": raw.get("golden_set", "")
                    + "@"
                    + str(raw.get("duration_seconds", "")),
                    "ts": datetime.now(tz=UTC).isoformat(),
                }
                team.quality_scores = current
                typer.echo(f"ok\t{team_id}\t{fqmn}\tscore={float(score):.3f} n={n_samples}")
        finally:
            await engine.dispose()

    _run(_do())


def _parse_eval_summary(
    raw: dict[str, Any],
) -> tuple[str | None, float | None, int]:
    """Pull (candidate fqmn, mean score, n_samples) from an eval JSON.

    Supports both shapes:

    - **Single-judge** (``EvalRunSummary.to_dict``): top-level
      ``candidate_model``, ``overall_mean``, ``scored_cases``.
    - **Multi-judge** (``MultiJudgeEvalSummary.to_dict``): top-level
      ``candidate_model``, ``per_judge`` list of ``{judge_id, mean,
      n_scored, ...}``. We average ``mean`` across judges and take
      the min ``n_scored`` as the sample count (most conservative).

    Returns ``(None, None, 0)`` when neither shape matches — caller
    should treat as a parse failure and abort.
    """
    candidate = raw.get("candidate_model")
    if not isinstance(candidate, str) or not candidate:
        return None, None, 0

    # Multi-judge: per_judge is present.
    per_judge = raw.get("per_judge")
    if isinstance(per_judge, list) and per_judge:
        means: list[float] = []
        n_samples_seen: list[int] = []
        for entry in per_judge:
            if not isinstance(entry, dict):
                continue
            mean = entry.get("mean")
            n = entry.get("n_scored")
            if isinstance(mean, int | float) and isinstance(n, int):
                means.append(float(mean))
                n_samples_seen.append(n)
        if means:
            avg = sum(means) / len(means)
            n_samples = min(n_samples_seen) if n_samples_seen else 0
            return candidate, avg, n_samples

    # Single-judge fallback.
    overall_mean = raw.get("overall_mean")
    scored_cases = raw.get("scored_cases", 0)
    if isinstance(overall_mean, int | float):
        n = int(scored_cases) if isinstance(scored_cases, int) else 0
        return candidate, float(overall_mean), n

    return None, None, 0


# --------------------------------------------------------------------------- #
# team set-guardrail-policy (Phase 8.2)                                       #
# --------------------------------------------------------------------------- #


@team_app.command("set-guardrail-policy")
def team_set_guardrail_policy(
    id: str,
    disable: Annotated[
        list[str],
        typer.Option(
            "--disable",
            help="Add a rule to the disabled list (skip it entirely). Repeatable.",
        ),
    ] = [],  # noqa: B006 — Typer interprets [] as 'no occurrences'
    enable: Annotated[
        list[str],
        typer.Option(
            "--enable",
            help="Remove a rule from the disabled list (return to engine default). Repeatable.",
        ),
    ] = [],  # noqa: B006
    set_action: Annotated[
        list[str],
        typer.Option(
            "--set-action",
            help="Override action for a rule. Format: RULE:ACTION (e.g. "
            "injection:block). Repeatable.",
        ),
    ] = [],  # noqa: B006
    reset: Annotated[
        bool,
        typer.Option("--reset", help="Clear ALL policy overrides for this team."),
    ] = False,
    show: Annotated[
        bool,
        typer.Option("--show", help="Print the current policy and exit."),
    ] = False,
) -> None:
    """Manage per-team guardrail policy overrides.

    The team's ``guardrail_policy`` JSON column lets you turn off
    specific rules or change their default action for one tenant
    without redeploying the gateway. Useful when a particular endpoint
    or use-case has documented quality regressions from a rule
    (e.g. PII redaction over-firing on topically-relevant tokens).

    Examples
    --------
        pronaos-cli team set-guardrail-policy <id> --disable pii.ipv4
        pronaos-cli team set-guardrail-policy <id> --set-action injection:block
        pronaos-cli team set-guardrail-policy <id> --enable pii.ipv4
        pronaos-cli team set-guardrail-policy <id> --reset
        pronaos-cli team set-guardrail-policy <id> --show
    """
    from pronaos.guardrails.policy import validate_policy

    async def _do() -> None:
        engine = create_engine(get_settings())
        sm = create_sessionmaker(engine)
        try:
            async with get_session(sm) as session:
                team = await session.get(Team, id)
                if team is None:
                    typer.echo(f"team not found: {id}", err=True)
                    raise typer.Exit(code=1)

                # Start from existing policy or an empty shell.
                policy: dict[str, Any] = dict(team.guardrail_policy or {})

                if reset:
                    team.guardrail_policy = None
                    typer.echo(f"ok\t{id}\tpolicy=reset")
                    if show:
                        typer.echo("(no policy override; using engine defaults)")
                    return

                if show:
                    if not team.guardrail_policy:
                        typer.echo("(no policy override; using engine defaults)")
                    else:
                        import json as _json

                        typer.echo(_json.dumps(team.guardrail_policy, indent=2))
                    return

                # Apply --disable / --enable to disabled_rules list.
                disabled = set(policy.get("disabled_rules", []))
                disabled.update(disable)
                disabled.difference_update(enable)
                if disabled:
                    policy["disabled_rules"] = sorted(disabled)
                elif "disabled_rules" in policy:
                    del policy["disabled_rules"]

                # Apply --set-action to rule_actions map.
                actions = dict(policy.get("rule_actions", {}))
                for entry in set_action:
                    if ":" not in entry:
                        typer.echo(
                            f"error: --set-action expects RULE:ACTION, got {entry!r}",
                            err=True,
                        )
                        raise typer.Exit(code=2)
                    rule, action = entry.split(":", 1)
                    actions[rule.strip()] = action.strip().lower()
                if actions:
                    policy["rule_actions"] = actions
                elif "rule_actions" in policy:
                    del policy["rule_actions"]

                # Validate before persisting — better to reject a typo here
                # than have the resolver silently drop it at request time.
                errors = validate_policy(policy or None)
                if errors:
                    typer.echo("error: invalid policy", err=True)
                    for e in errors:
                        typer.echo(f"  - {e}", err=True)
                    raise typer.Exit(code=2)

                team.guardrail_policy = policy or None
                display = "(default)" if team.guardrail_policy is None else "set"
                typer.echo(f"ok\t{id}\tpolicy={display}")
                if team.guardrail_policy:
                    import json as _json

                    typer.echo(_json.dumps(team.guardrail_policy, indent=2))
        finally:
            await engine.dispose()

    _run(_do())


# --------------------------------------------------------------------------- #
# team set-allowed-models (Phase 17)                                          #
# --------------------------------------------------------------------------- #


@team_app.command("set-allowed-models")
def team_set_allowed_models(
    id: str,
    models: Annotated[
        str | None,
        typer.Option(
            "--models",
            help=(
                "Comma-separated list of fnmatch patterns the team is "
                "allowed to invoke. Examples: 'groq/*' or "
                "'groq/*,anthropic/claude-opus-*' or "
                "'groq/llama-3.1-8b-instant'. Pass an empty string to "
                "explicitly deny everything (paused team)."
            ),
        ),
    ] = None,
    clear: Annotated[
        bool,
        typer.Option(
            "--clear",
            help="Clear the allowlist (NULL → team is unrestricted again).",
        ),
    ] = False,
    show: Annotated[
        bool,
        typer.Option("--show", help="Print the current allowlist and exit."),
    ] = False,
) -> None:
    """Manage per-team model allowlists.

    The team's ``allowed_models`` JSON column gates which models its
    API keys may invoke. NULL = unrestricted (default for new teams);
    a non-empty list restricts to matching patterns; an empty list
    ``[]`` denies everything (useful for pausing a team without
    revoking its keys).

    Examples
    --------
        # Only let this team use Groq's free tier:
        pronaos-cli team set-allowed-models <id> --models 'groq/*'

        # Multiple patterns:
        pronaos-cli team set-allowed-models <id> \\
            --models 'groq/*,anthropic/claude-opus-*'

        # Pause the team (deny all model access; auth still works):
        pronaos-cli team set-allowed-models <id> --models ''

        # Lift the restriction entirely:
        pronaos-cli team set-allowed-models <id> --clear

        # Read the current policy:
        pronaos-cli team set-allowed-models <id> --show
    """
    from pronaos.core.model_access import validate_allowed_models

    async def _do() -> None:
        engine = create_engine(get_settings())
        sm = create_sessionmaker(engine)
        try:
            async with get_session(sm) as session:
                team = await session.get(Team, id)
                if team is None:
                    typer.echo(f"team not found: {id}", err=True)
                    raise typer.Exit(code=1)

                if show:
                    if team.allowed_models is None:
                        typer.echo("(unrestricted — no allowlist set)")
                    elif not team.allowed_models:
                        typer.echo("(deny-all — empty list)")
                    else:
                        for pattern in team.allowed_models:
                            typer.echo(pattern)
                    return

                if clear:
                    team.allowed_models = None
                    typer.echo(f"ok\t{id}\tallowed_models=unrestricted")
                    return

                if models is None:
                    typer.echo(
                        "error: pass --models, --clear, or --show",
                        err=True,
                    )
                    raise typer.Exit(code=2)

                # Parse the comma-separated input. Empty string → []
                # (the explicit deny-all policy).
                if models.strip() == "":
                    patterns: list[str] = []
                else:
                    patterns = [p.strip() for p in models.split(",") if p.strip()]

                # Validate before write. The validator raises ValueError
                # with a human-readable reason; bubble it to stderr +
                # exit 2 to mirror the guardrail-policy CLI's UX.
                try:
                    validate_allowed_models(patterns)
                except ValueError as e:
                    typer.echo(f"error: invalid allowed_models: {e}", err=True)
                    raise typer.Exit(code=2) from None

                team.allowed_models = patterns
                if not patterns:
                    typer.echo(f"ok\t{id}\tallowed_models=deny-all")
                else:
                    typer.echo(f"ok\t{id}\tallowed_models=" + ",".join(patterns))
        finally:
            await engine.dispose()

    _run(_do())


# --------------------------------------------------------------------------- #
# team set-routing-strategy (Phase 21)                                        #
# --------------------------------------------------------------------------- #


@team_app.command("set-routing-strategy")
def team_set_routing_strategy(
    id: str,
    strategy: Annotated[
        str | None,
        typer.Option(
            "--strategy",
            help=(
                "Routing strategy applied to model='auto' requests: "
                "'cheapest' (minimise expected cost), 'fastest' "
                "(minimise typical p50 latency), or 'balanced' "
                "(normalised cost+latency)."
            ),
        ),
    ] = None,
    clear: Annotated[
        bool,
        typer.Option(
            "--clear",
            help="Clear the strategy (NULL → falls back to 'cheapest').",
        ),
    ] = False,
    show: Annotated[
        bool,
        typer.Option("--show", help="Print the current strategy and exit."),
    ] = False,
) -> None:
    """Set the per-team routing strategy for ``model="auto"`` requests.

    When a client sends ``model="auto"``, the gateway picks a concrete
    provider/model from the team's allowlist using this strategy. NULL
    column = no preference; the gateway defaults to ``cheapest``.

    Examples
    --------
        pronaos-cli team set-routing-strategy <id> --strategy cheapest
        pronaos-cli team set-routing-strategy <id> --strategy fastest
        pronaos-cli team set-routing-strategy <id> --strategy balanced
        pronaos-cli team set-routing-strategy <id> --clear
        pronaos-cli team set-routing-strategy <id> --show
    """
    from pronaos.core.scorer import RoutingStrategy

    async def _do() -> None:
        engine = create_engine(get_settings())
        sm = create_sessionmaker(engine)
        try:
            async with get_session(sm) as session:
                team = await session.get(Team, id)
                if team is None:
                    typer.echo(f"team not found: {id}", err=True)
                    raise typer.Exit(code=1)

                if show:
                    if team.routing_strategy is None:
                        typer.echo("(unset — defaults to 'cheapest')")
                    else:
                        typer.echo(team.routing_strategy)
                    return

                if clear:
                    team.routing_strategy = None
                    typer.echo(f"ok\t{id}\trouting_strategy=unset")
                    return

                if strategy is None:
                    typer.echo(
                        "error: pass --strategy, --clear, or --show",
                        err=True,
                    )
                    raise typer.Exit(code=2)

                try:
                    parsed = RoutingStrategy(strategy.strip().lower())
                except ValueError:
                    typer.echo(
                        "error: invalid strategy. Expected one of: "
                        + ", ".join(s.value for s in RoutingStrategy),
                        err=True,
                    )
                    raise typer.Exit(code=2) from None

                team.routing_strategy = parsed.value
                typer.echo(f"ok\t{id}\trouting_strategy={parsed.value}")
        finally:
            await engine.dispose()

    _run(_do())


@team_app.command("set-hedge-policy")
def team_set_hedge_policy(
    id: str,
    delay_ms: Annotated[
        float | None,
        typer.Option(
            "--delay-ms",
            help=(
                "Wall-clock delay (ms) the failover executor waits for the "
                "primary provider before speculatively starting an "
                "identical call against the next provider in the chain. "
                "Typical setting is roughly the primary's p50 latency. "
                "0 or unset = no hedging (default sequential failover)."
            ),
        ),
    ] = None,
    max_count: Annotated[
        int | None,
        typer.Option(
            "--max-count",
            help=(
                "Cap on how many hedge candidates fire per request, "
                "regardless of chain length. Default 1 = race the primary "
                "against one alternative."
            ),
        ),
    ] = None,
    clear: Annotated[
        bool,
        typer.Option("--clear", help="Disable hedging entirely (NULL both columns)."),
    ] = False,
    show: Annotated[
        bool,
        typer.Option("--show", help="Print the current hedge policy and exit."),
    ] = False,
) -> None:
    """Set the per-team request-hedging policy for tail-latency reduction.

    Hedging starts a *speculative* identical call against the next
    provider in the chain after ``delay_ms`` if the primary hasn't
    returned yet, then returns whichever finishes first and cancels the
    loser. Honest cost overhead: roughly the hedge-trigger rate * the
    upstream cost (each cancelled call has typically generated zero or
    very few completion tokens before the cancel reaches the upstream).

    Reference: Dean & Barroso, "The Tail at Scale", CACM 2013.

    Examples
    --------
        pronaos-cli team set-hedge-policy <id> --delay-ms 250
        pronaos-cli team set-hedge-policy <id> --delay-ms 200 --max-count 2
        pronaos-cli team set-hedge-policy <id> --clear
        pronaos-cli team set-hedge-policy <id> --show
    """

    async def _do() -> None:
        engine = create_engine(get_settings())
        sm = create_sessionmaker(engine)
        try:
            async with get_session(sm) as session:
                team = await session.get(Team, id)
                if team is None:
                    typer.echo(f"team not found: {id}", err=True)
                    raise typer.Exit(code=1)

                if show:
                    if team.hedge_delay_ms is None:
                        typer.echo("(unset — hedging disabled)")
                    else:
                        n = team.hedge_max_count if team.hedge_max_count is not None else 1
                        typer.echo(f"delay_ms={team.hedge_delay_ms} max_count={n}")
                    return

                if clear:
                    team.hedge_delay_ms = None
                    team.hedge_max_count = None
                    typer.echo(f"ok\t{id}\thedge=disabled")
                    return

                if delay_ms is None and max_count is None:
                    typer.echo(
                        "error: pass --delay-ms, --max-count, --clear, or --show",
                        err=True,
                    )
                    raise typer.Exit(code=2)

                if delay_ms is not None:
                    if delay_ms < 0:
                        typer.echo("error: --delay-ms must be >= 0", err=True)
                        raise typer.Exit(code=2)
                    team.hedge_delay_ms = delay_ms
                if max_count is not None:
                    if max_count < 0:
                        typer.echo("error: --max-count must be >= 0", err=True)
                        raise typer.Exit(code=2)
                    team.hedge_max_count = max_count

                effective_n = team.hedge_max_count if team.hedge_max_count is not None else 1
                typer.echo(
                    f"ok\t{id}\thedge_delay_ms={team.hedge_delay_ms}\thedge_max_count={effective_n}"
                )
        finally:
            await engine.dispose()

    _run(_do())


# --------------------------------------------------------------------------- #
# audit — hash-chained audit log (Phase 10)                                   #
# --------------------------------------------------------------------------- #


@audit_app.command("verify")
def audit_verify(
    tenant: Annotated[
        str,
        typer.Option(
            "--tenant",
            "-t",
            help="Tenant id whose chain to verify.",
        ),
    ],
) -> None:
    """Walk a tenant's audit chain and report tamper points.

    Exit code 0 = chain intact. Non-zero = at least one break.
    Designed to be CI-friendly (run nightly against staging or
    production, alert on non-zero exit).

    Example
    -------
        pronaos-cli audit verify --tenant 1743243380104bf4839758939077621e
    """

    async def _do() -> None:
        from pronaos.audit.verifier import AuditVerifier
        from pronaos.core.webhooks import (
            WebhookConfig,
            WebhookDispatcher,
            audit_chain_broken_event,
        )
        from pronaos.db.models import Tenant

        engine = create_engine(get_settings())
        sm = create_sessionmaker(engine)
        dispatcher = WebhookDispatcher()
        try:
            verifier = AuditVerifier()
            async with get_session(sm) as session:
                result = await verifier.verify(session, tenant)

                # Phase 19: fire webhook events for each detected break.
                # The CLI is the natural publish-point for chain-break
                # events because chain verification is an out-of-band
                # operation (nightly cron / manual), not a request-time
                # check. Fetch the tenant's webhook config in the same
                # session to keep this best-effort.
                if not result.is_intact:
                    tenant_row = await session.get(Tenant, tenant)
                    if tenant_row is not None:
                        config = WebhookConfig(
                            url=tenant_row.webhook_url,
                            secret=tenant_row.webhook_secret,
                        )
                        for b in result.breaks:
                            dispatcher.publish(
                                config,
                                audit_chain_broken_event(
                                    tenant_id=tenant,
                                    record_id=b.record_id,
                                    reason=b.reason,
                                    ts=b.ts_iso,
                                ),
                            )

            typer.echo(f"tenant:           {result.tenant_id}")
            typer.echo(f"total records:    {result.total_records}")
            typer.echo(f"verified:         {result.verified_records}")
            typer.echo(f"breaks:           {len(result.breaks)}")
            typer.echo("")
            if result.is_intact:
                typer.echo(f"chain intact ({result.verified_records} records verified)")
            else:
                typer.echo("CHAIN BROKEN — first 5 breaks:")
                for b in result.breaks[:5]:
                    typer.echo(f"  - record {b.record_id} @ {b.ts_iso}: {b.reason}")
                    typer.echo(f"      expected: {b.expected_hash[:16]}...")
                    typer.echo(f"      actual:   {b.actual_hash[:16]}...")
                if len(result.breaks) > 5:
                    typer.echo(f"  ... and {len(result.breaks) - 5} more")
                # Drain any in-flight webhook deliveries before exiting,
                # so chain-break notifications aren't lost on a fast
                # exit. Best-effort with a short deadline.
                with contextlib.suppress(TimeoutError, Exception):
                    await asyncio.wait_for(dispatcher.aclose(), timeout=15.0)
                raise typer.Exit(code=1)
        finally:
            await dispatcher.aclose()
            await engine.dispose()

    _run(_do())


@audit_app.command("show")
def audit_show(
    tenant: Annotated[
        str,
        typer.Option("--tenant", "-t", help="Tenant id to show records for."),
    ],
    limit: Annotated[
        int, typer.Option("--limit", "-n", help="How many recent records to show.")
    ] = 10,
) -> None:
    """Print the N most recent audit records for a tenant.

    Bodies are NOT stored (only hashes); use this to cross-reference
    request_id with separately-kept application logs to retrieve the
    original prompt/response if needed.
    """

    async def _do() -> None:
        from pronaos.db.models import AuditRecord

        engine = create_engine(get_settings())
        sm = create_sessionmaker(engine)
        try:
            async with get_session(sm) as session:
                stmt = (
                    select(AuditRecord)
                    .where(AuditRecord.tenant_id == tenant)
                    .order_by(AuditRecord.ts.desc())
                    .limit(limit)
                )
                rows = (await session.execute(stmt)).scalars().all()

            if not rows:
                typer.echo(f"no audit records for tenant {tenant}")
                return

            typer.echo(f"{'ts':<26} {'model':<38} {'this_hash':<18} prev_hash")
            for r in rows:
                this_short = r.this_hash[:16] + ".."
                prev_short = (r.prev_hash[:16] + "..") if r.prev_hash else "(genesis)"
                typer.echo(
                    f"{r.ts.isoformat()[:26]:<26} {r.model:<38} {this_short:<18} {prev_short}"
                )
        finally:
            await engine.dispose()

    _run(_do())


# --------------------------------------------------------------------------- #
# team set-agent-budget — per-execution budget for tool-using agents (Phase 30) #
# --------------------------------------------------------------------------- #


@team_app.command("set-agent-budget")
def team_set_agent_budget(
    id: str,
    tokens: Annotated[
        int | None,
        typer.Option(
            "--tokens",
            help=(
                "Cumulative tokens (prompt+completion) allowed under one "
                "X-Pronaos-Agent-Turn-ID. NULL = unlimited."
            ),
        ),
    ] = None,
    cost_hcents: Annotated[
        int | None,
        typer.Option(
            "--cost-hcents",
            help=(
                "Cumulative cost (hundredths-of-a-cent) allowed under one "
                "agent turn. NULL = unlimited."
            ),
        ),
    ] = None,
    ttl: Annotated[
        int | None,
        typer.Option(
            "--ttl",
            help=(
                "How long (seconds) the per-turn counters persist in Redis "
                "after the last write. NULL = 3600 (one hour)."
            ),
        ),
    ] = None,
    clear: Annotated[
        bool,
        typer.Option("--clear", help="Clear all three columns (back to unlimited)."),
    ] = False,
    show: Annotated[
        bool,
        typer.Option("--show", help="Print the current agent-turn budget config."),
    ] = False,
) -> None:
    """Set the team's per-agent-turn budget (Phase 30).

    Clients building tool-using agent loops send the same
    ``X-Pronaos-Agent-Turn-ID`` header on every call belonging to one
    logical execution. The gateway accumulates running token + cost
    totals per turn-id and denies the call that would push the team
    over either limit. NULL on either budget column = unlimited; the
    matching gate is a no-op.

    Examples
    --------
        # Cap a single agent execution at 5000 tokens:
        pronaos-cli team set-agent-budget <id> --tokens 5000

        # Or by cost (50 hcents = $0.005):
        pronaos-cli team set-agent-budget <id> --cost-hcents 50

        # Tighter TTL for short-lived agents:
        pronaos-cli team set-agent-budget <id> --tokens 5000 --ttl 600
    """

    async def _do() -> None:
        engine = create_engine(get_settings())
        sm = create_sessionmaker(engine)
        try:
            async with get_session(sm) as session:
                team = await session.get(Team, id)
                if team is None:
                    typer.echo(f"team not found: {id}", err=True)
                    raise typer.Exit(code=1)

                if show:
                    typer.echo(f"agent_turn_budget_tokens:        {team.agent_turn_budget_tokens}")
                    typer.echo(
                        f"agent_turn_budget_cost_hcents:   {team.agent_turn_budget_cost_hcents}"
                    )
                    typer.echo(f"agent_turn_ttl_seconds:          {team.agent_turn_ttl_seconds}")
                    return

                if clear:
                    team.agent_turn_budget_tokens = None
                    team.agent_turn_budget_cost_hcents = None
                    team.agent_turn_ttl_seconds = None
                    typer.echo(f"ok\t{id}\tagent-budget=cleared")
                    return

                if tokens is None and cost_hcents is None and ttl is None:
                    typer.echo(
                        "error: pass --tokens, --cost-hcents, --ttl, --clear, or --show",
                        err=True,
                    )
                    raise typer.Exit(code=2)

                if tokens is not None:
                    if tokens < 0:
                        typer.echo("error: --tokens must be >= 0", err=True)
                        raise typer.Exit(code=2)
                    team.agent_turn_budget_tokens = tokens
                if cost_hcents is not None:
                    if cost_hcents < 0:
                        typer.echo("error: --cost-hcents must be >= 0", err=True)
                        raise typer.Exit(code=2)
                    team.agent_turn_budget_cost_hcents = cost_hcents
                if ttl is not None:
                    if ttl < 1:
                        typer.echo("error: --ttl must be >= 1", err=True)
                        raise typer.Exit(code=2)
                    team.agent_turn_ttl_seconds = ttl

                typer.echo(
                    f"ok\t{id}\ttokens={team.agent_turn_budget_tokens}\t"
                    f"cost_hcents={team.agent_turn_budget_cost_hcents}\t"
                    f"ttl={team.agent_turn_ttl_seconds}"
                )
        finally:
            await engine.dispose()

    _run(_do())


# --------------------------------------------------------------------------- #
# team set-tool-budget — per-tool call budgets (Phase 37)                     #
# --------------------------------------------------------------------------- #


@team_app.command("set-tool-budget")
def team_set_tool_budget(
    id: str,
    tool: Annotated[
        str | None,
        typer.Option(
            "--tool",
            help=(
                "Tool name to cap (the function name the LLM emits in "
                "tool_calls, e.g. 'web_search'). Required unless --clear."
            ),
        ),
    ] = None,
    limit: Annotated[
        int | None,
        typer.Option(
            "--limit",
            help=(
                "Cap on the number of LLM-emitted invocations of this "
                "tool per monthly period. Once reached, the gateway "
                "strips the tool from outgoing requests until rollover. "
                "Pass 0 to disable the cap without removing the entry."
            ),
        ),
    ] = None,
    reset: Annotated[
        bool,
        typer.Option("--reset", help="Reset current_calls to 0 for this tool (keep limit)."),
    ] = False,
    remove: Annotated[
        bool,
        typer.Option("--remove", help="Delete the entry for this tool (back to uncapped)."),
    ] = False,
    clear: Annotated[
        bool,
        typer.Option("--clear", help="Drop ALL tool budgets for this team (back to uncapped)."),
    ] = False,
    show: Annotated[
        bool,
        typer.Option("--show", help="Print the current per-tool budgets."),
    ] = False,
) -> None:
    """Set or inspect the team's per-tool call budgets (Phase 37).

    Per-tool budgets cap the count of tool_call EMISSIONS the LLM
    produces for each named tool within the team's monthly period.
    Enforcement is **strip-by-removal**: when the count reaches the
    cap, the chat handler removes the tool from the upstream request's
    ``tools`` array before forwarding. The LLM never sees the tool
    and never wastes reasoning on a denied invocation.

    Examples
    --------
        # Cap web_search at 100 calls/month:
        pronaos-cli team set-tool-budget <id> --tool web_search --limit 100

        # Reset the running counter mid-period (e.g. after an incident):
        pronaos-cli team set-tool-budget <id> --tool web_search --reset

        # Remove the cap entirely:
        pronaos-cli team set-tool-budget <id> --tool web_search --remove

        # Drop all caps:
        pronaos-cli team set-tool-budget <id> --clear

        # Inspect current state:
        pronaos-cli team set-tool-budget <id> --show
    """

    async def _do() -> None:
        engine = create_engine(get_settings())
        sm = create_sessionmaker(engine)
        try:
            async with get_session(sm) as session:
                team = await session.get(Team, id)
                if team is None:
                    typer.echo(f"team not found: {id}", err=True)
                    raise typer.Exit(code=1)

                if show:
                    budgets = team.tool_budgets or {}
                    if not budgets:
                        typer.echo("no per-tool budgets configured")
                        return
                    typer.echo(f"{'tool':32s}  {'current':>10s} / {'limit':>10s}")
                    for name, entry in sorted(budgets.items()):
                        if not isinstance(entry, dict):
                            continue
                        current = entry.get("current_calls", 0)
                        cap = entry.get("limit_calls", 0)
                        typer.echo(f"{name:32s}  {current:>10d} / {cap:>10d}")
                    return

                if clear:
                    team.tool_budgets = None
                    typer.echo(f"ok\t{id}\ttool-budgets=cleared")
                    return

                if tool is None:
                    typer.echo(
                        "error: pass --tool NAME with --limit, --reset, --remove "
                        "(or use --clear/--show without --tool)",
                        err=True,
                    )
                    raise typer.Exit(code=2)

                # Read-modify-write. Coerce NULL to {} so subsequent
                # ops never operate on None.
                existing = dict(team.tool_budgets or {})

                if remove:
                    existing.pop(tool, None)
                    team.tool_budgets = existing or None
                    typer.echo(f"ok\t{id}\ttool={tool}\tremoved")
                    return

                entry = dict(existing.get(tool, {}))
                if reset:
                    entry["current_calls"] = 0
                if limit is not None:
                    if limit < 0:
                        typer.echo("error: --limit must be >= 0", err=True)
                        raise typer.Exit(code=2)
                    entry["limit_calls"] = limit
                    # Initialise current_calls to 0 on first write so the
                    # gate has a sensible baseline. Pre-existing entries
                    # keep their running counter.
                    entry.setdefault("current_calls", 0)

                if not entry:
                    typer.echo(
                        "error: nothing to do; pass --limit, --reset, --remove, or --clear",
                        err=True,
                    )
                    raise typer.Exit(code=2)

                existing[tool] = entry
                team.tool_budgets = existing

                typer.echo(
                    f"ok\t{id}\ttool={tool}\t"
                    f"current={entry.get('current_calls', 0)}\t"
                    f"limit={entry.get('limit_calls', 0)}"
                )
        finally:
            await engine.dispose()

    _run(_do())


# --------------------------------------------------------------------------- #
# team set-pii-tokenization — reversible PII tokenization config (Phase 38)   #
# --------------------------------------------------------------------------- #


@team_app.command("set-pii-tokenization")
def team_set_pii_tokenization(
    id: str,
    enable: Annotated[
        bool,
        typer.Option("--enable", help="Turn tokenization ON for this team."),
    ] = False,
    disable: Annotated[
        bool,
        typer.Option("--disable", help="Turn tokenization OFF for this team."),
    ] = False,
    ttl: Annotated[
        int | None,
        typer.Option(
            "--ttl",
            help=(
                "TTL (seconds) on each (token -> original) mapping in Redis. "
                "NULL = use the gateway default (3600s)."
            ),
        ),
    ] = None,
    show: Annotated[
        bool,
        typer.Option("--show", help="Print the current tokenization config."),
    ] = False,
) -> None:
    """Configure reversible PII tokenization for this team (Phase 38).

    With tokenization OFF (default), PII matched by guardrail rules
    is REDACTED — replaced with a generic marker, one-way, lossy.
    With tokenization ON, rules whose per-team policy maps them to
    ``"tokenize"`` produce deterministic ``[TYPE_HASH]`` tokens; the
    gateway holds the mapping in Redis with the configured TTL and
    reverses tokens in the upstream response before returning to the
    client. The upstream LLM never sees originals; the client gets
    real data back.

    Tokenization requires BOTH this flag AND a rule-level
    ``"tokenize"`` action in the team's ``guardrail_policy``. Either
    one missing = the rule falls back to REDACT (safe default).

    Examples
    --------
        # Turn on with default 1-hour TTL:
        pronaos-cli team set-pii-tokenization <id> --enable

        # Tighter TTL for short-lived agent loops:
        pronaos-cli team set-pii-tokenization <id> --enable --ttl 600

        # Turn off (revert to redaction):
        pronaos-cli team set-pii-tokenization <id> --disable

        # Inspect:
        pronaos-cli team set-pii-tokenization <id> --show
    """

    async def _do() -> None:
        engine = create_engine(get_settings())
        sm = create_sessionmaker(engine)
        try:
            async with get_session(sm) as session:
                team = await session.get(Team, id)
                if team is None:
                    typer.echo(f"team not found: {id}", err=True)
                    raise typer.Exit(code=1)

                if show:
                    typer.echo(f"pii_tokenization_enabled: {team.pii_tokenization_enabled}")
                    typer.echo(f"pii_token_ttl_seconds:    {team.pii_token_ttl_seconds}")
                    return

                if enable and disable:
                    typer.echo("error: --enable and --disable are mutually exclusive", err=True)
                    raise typer.Exit(code=2)
                if not enable and not disable and ttl is None:
                    typer.echo("error: pass --enable, --disable, --ttl, or --show", err=True)
                    raise typer.Exit(code=2)

                if enable:
                    team.pii_tokenization_enabled = True
                if disable:
                    team.pii_tokenization_enabled = False
                if ttl is not None:
                    if ttl < 1:
                        typer.echo("error: --ttl must be >= 1", err=True)
                        raise typer.Exit(code=2)
                    team.pii_token_ttl_seconds = ttl

                typer.echo(
                    f"ok\t{id}\tenabled={team.pii_tokenization_enabled}\t"
                    f"ttl={team.pii_token_ttl_seconds}"
                )
        finally:
            await engine.dispose()

    _run(_do())


# --------------------------------------------------------------------------- #
# team set-structured-output — JSON Schema validation + retry (Phase 39)      #
# --------------------------------------------------------------------------- #


@team_app.command("set-structured-output")
def team_set_structured_output(
    id: str,
    max_retries: Annotated[
        int | None,
        typer.Option(
            "--max-retries",
            help=(
                "Cap on how many times the gateway re-fires a completion "
                "when the LLM's response fails JSON Schema validation. "
                "0 disables auto-retry; default is 2."
            ),
        ),
    ] = None,
    provider_native: Annotated[
        bool | None,
        typer.Option(
            "--provider-native/--no-provider-native",
            help=(
                "When True, forward the JSON Schema to the upstream "
                "provider's native structured-output mechanism (OpenAI "
                "response_format). When False, fall back to "
                "schema-guided prompting for every provider."
            ),
        ),
    ] = None,
    show: Annotated[
        bool,
        typer.Option("--show", help="Print the current structured-output config."),
    ] = False,
) -> None:
    """Configure structured-output validation + auto-retry (Phase 39).

    When a client sends a JSON Schema with their request, the gateway
    validates the LLM's response against the schema and auto-retries
    with a corrective prompt on violation. This caps the
    "every team writes their own validate+retry loop" pattern at the
    gateway tier.

    Examples
    --------
        # Tighter retry budget (cost-sensitive workload):
        pronaos-cli team set-structured-output <id> --max-retries 1

        # Disable retries entirely (treat violations as bugs):
        pronaos-cli team set-structured-output <id> --max-retries 0

        # Force prompt-injection fallback (provider native has known bugs):
        pronaos-cli team set-structured-output <id> --no-provider-native

        # Inspect:
        pronaos-cli team set-structured-output <id> --show
    """

    async def _do() -> None:
        engine = create_engine(get_settings())
        sm = create_sessionmaker(engine)
        try:
            async with get_session(sm) as session:
                team = await session.get(Team, id)
                if team is None:
                    typer.echo(f"team not found: {id}", err=True)
                    raise typer.Exit(code=1)

                if show:
                    retries = team.structured_output_max_retries
                    native = team.structured_output_provider_native
                    typer.echo(f"structured_output_max_retries:     {retries}")
                    typer.echo(f"structured_output_provider_native: {native}")
                    return

                if max_retries is None and provider_native is None:
                    typer.echo(
                        "error: pass --max-retries N, "
                        "--provider-native / --no-provider-native, or --show",
                        err=True,
                    )
                    raise typer.Exit(code=2)

                if max_retries is not None:
                    if max_retries < 0:
                        typer.echo("error: --max-retries must be >= 0", err=True)
                        raise typer.Exit(code=2)
                    team.structured_output_max_retries = max_retries
                if provider_native is not None:
                    team.structured_output_provider_native = provider_native

                typer.echo(
                    f"ok\t{id}\t"
                    f"max_retries={team.structured_output_max_retries}\t"
                    f"provider_native={team.structured_output_provider_native}"
                )
        finally:
            await engine.dispose()

    _run(_do())


# --------------------------------------------------------------------------- #
# team set-quality-monitor — production quality sampling (Phase 40)            #
# --------------------------------------------------------------------------- #


@team_app.command("set-quality-monitor")
def team_set_quality_monitor(
    id: str,
    sampling_rate: Annotated[
        float | None,
        typer.Option(
            "--sampling-rate",
            help=(
                "Probability that any production response gets sampled "
                "and scored by the judge model. 0.0 = sampling off; "
                "0.01 = 1% (operationally common); 1.0 = score every "
                "response (only for low-volume teams)."
            ),
        ),
    ] = None,
    judge_model: Annotated[
        str | None,
        typer.Option(
            "--judge-model",
            help=(
                "Override the gateway-wide default judge model "
                "(typically gpt-4o-mini). Use the fqmn shape "
                "(e.g. openai/gpt-4o-mini, groq/llama-3.1-70b-versatile)."
            ),
        ),
    ] = None,
    clear_judge: Annotated[
        bool,
        typer.Option(
            "--clear-judge",
            help="Clear the team's judge model override, falling back to default.",
        ),
    ] = False,
    show: Annotated[
        bool,
        typer.Option("--show", help="Print current sampling config + degradation state."),
    ] = False,
) -> None:
    """Configure quality-regression monitoring (Phase 40).

    Sampling-rate is the probability per response that the gateway
    fires a judge call to score it. The score is persisted in
    ``quality_samples`` and triggers a Welch's t-test against the
    team's stored baseline (from ``pronaos-cli eval store-scores``).
    When the test detects significant degradation (p < 0.05), the
    model is marked degraded; the ``model="auto"`` router excludes
    it from the candidate pool until the next check shows recovery.

    Examples
    --------
        # Turn on 1% sampling:
        pronaos-cli team set-quality-monitor <id> --sampling-rate 0.01

        # Use a specific judge (cost optimisation):
        pronaos-cli team set-quality-monitor <id> --judge-model groq/llama-3.1-70b-versatile

        # Inspect current state + active degradations:
        pronaos-cli team set-quality-monitor <id> --show

        # Disable:
        pronaos-cli team set-quality-monitor <id> --sampling-rate 0.0
    """

    async def _do() -> None:
        engine = create_engine(get_settings())
        sm = create_sessionmaker(engine)
        try:
            async with get_session(sm) as session:
                team = await session.get(Team, id)
                if team is None:
                    typer.echo(f"team not found: {id}", err=True)
                    raise typer.Exit(code=1)

                if show:
                    typer.echo(f"quality_sampling_rate: {team.quality_sampling_rate}")
                    typer.echo(f"quality_judge_model:   {team.quality_judge_model or '(default)'}")
                    state = team.model_degradation_state or {}
                    if state:
                        typer.echo("degradation state:")
                        for fqmn, entry in sorted(state.items()):
                            if not isinstance(entry, dict):
                                continue
                            flag = "DEGRADED" if entry.get("degraded") else "healthy"
                            rec_mean = entry.get("recent_mean", "?")
                            base_mean = entry.get("baseline_mean", "?")
                            p = entry.get("p_value", "?")
                            typer.echo(
                                f"  {fqmn:48s}  {flag:8s}  "
                                f"recent={rec_mean}  baseline={base_mean}  p={p}"
                            )
                    else:
                        typer.echo("degradation state: (none — no transitions yet)")
                    return

                if sampling_rate is None and judge_model is None and not clear_judge:
                    typer.echo(
                        "error: pass --sampling-rate, --judge-model, --clear-judge, or --show",
                        err=True,
                    )
                    raise typer.Exit(code=2)

                if sampling_rate is not None:
                    if not 0.0 <= sampling_rate <= 1.0:
                        typer.echo("error: --sampling-rate must be in [0.0, 1.0]", err=True)
                        raise typer.Exit(code=2)
                    team.quality_sampling_rate = sampling_rate
                if clear_judge:
                    team.quality_judge_model = None
                elif judge_model is not None:
                    team.quality_judge_model = judge_model

                typer.echo(
                    f"ok\t{id}\t"
                    f"sampling_rate={team.quality_sampling_rate}\t"
                    f"judge={team.quality_judge_model or '(default)'}"
                )
        finally:
            await engine.dispose()

    _run(_do())


# --------------------------------------------------------------------------- #
# team set-image-cap — per-team max base64 image bytes (Phase 41)             #
# --------------------------------------------------------------------------- #


@team_app.command("set-image-cap")
def team_set_image_cap(
    id: str,
    max_bytes: Annotated[
        int | None,
        typer.Option(
            "--max-bytes",
            help=(
                "Maximum total base64 image payload (in bytes) allowed in "
                "a single request. URL-based images aren't counted — the "
                "gateway doesn't fetch URLs. Use --clear to remove."
            ),
        ),
    ] = None,
    clear: Annotated[
        bool,
        typer.Option("--clear", help="Clear the cap (back to unlimited)."),
    ] = False,
    show: Annotated[
        bool,
        typer.Option("--show", help="Print the current image cap."),
    ] = False,
) -> None:
    """Set or inspect the team's max image bytes per request (Phase 41).

    Prevents a single request from running up a $50 bill by attaching
    a 100 MB image. The cap is enforced PRE-FLIGHT — the chat handler
    rejects with HTTP 422 before any upstream call is made.

    Examples
    --------
        # Cap at 5 MiB:
        pronaos-cli team set-image-cap <id> --max-bytes 5242880

        # Lift the cap:
        pronaos-cli team set-image-cap <id> --clear

        # Inspect:
        pronaos-cli team set-image-cap <id> --show
    """

    async def _do() -> None:
        engine = create_engine(get_settings())
        sm = create_sessionmaker(engine)
        try:
            async with get_session(sm) as session:
                team = await session.get(Team, id)
                if team is None:
                    typer.echo(f"team not found: {id}", err=True)
                    raise typer.Exit(code=1)

                if show:
                    typer.echo(f"max_image_bytes: {team.max_image_bytes}")
                    return

                if clear:
                    team.max_image_bytes = None
                    typer.echo(f"ok\t{id}\tmax_image_bytes=null (cleared)")
                    return

                if max_bytes is None:
                    typer.echo(
                        "error: pass --max-bytes N, --clear, or --show",
                        err=True,
                    )
                    raise typer.Exit(code=2)
                if max_bytes < 0:
                    typer.echo("error: --max-bytes must be >= 0", err=True)
                    raise typer.Exit(code=2)
                team.max_image_bytes = max_bytes
                typer.echo(f"ok\t{id}\tmax_image_bytes={team.max_image_bytes}")
        finally:
            await engine.dispose()

    _run(_do())


@team_app.command("set-batches")
def team_set_batches(
    id: str,
    enable: Annotated[
        bool,
        typer.Option("--enable", help="Allow this team to call /v1/batches."),
    ] = False,
    disable: Annotated[
        bool,
        typer.Option(
            "--disable",
            help="Block /v1/batches calls (back to 422 batches_disabled).",
        ),
    ] = False,
    show: Annotated[
        bool,
        typer.Option("--show", help="Print the team's current batches flag."),
    ] = False,
) -> None:
    """Toggle the team's async-batches API access (Phase 59).

    Batches submit large workloads at 50% of synchronous pricing
    with a 24-hour completion window. The default is OFF because
    batch quota usage is non-trivial and operators want explicit
    opt-in. When ON, the team can:

    - POST /v1/batches (submit a batch of chat completions)
    - GET /v1/batches/{id} (poll status)
    - GET /v1/batches/{id}/results (fetch results once completed)
    - POST /v1/batches/{id}/cancel (best-effort cancel)

    Examples
    --------
        # Enable:
        pronaos-cli team set-batches <id> --enable

        # Disable:
        pronaos-cli team set-batches <id> --disable

        # Inspect:
        pronaos-cli team set-batches <id> --show
    """

    async def _do() -> None:
        if enable and disable:
            typer.echo("error: pass exactly one of --enable / --disable", err=True)
            raise typer.Exit(code=2)
        engine = create_engine(get_settings())
        sm = create_sessionmaker(engine)
        try:
            async with get_session(sm) as session:
                team = await session.get(Team, id)
                if team is None:
                    typer.echo(f"team not found: {id}", err=True)
                    raise typer.Exit(code=1)

                if show:
                    typer.echo(f"batches_enabled: {team.batches_enabled}")
                    return

                if enable:
                    team.batches_enabled = True
                    typer.echo(f"ok\t{id}\tbatches_enabled=true")
                    return
                if disable:
                    team.batches_enabled = False
                    typer.echo(f"ok\t{id}\tbatches_enabled=false")
                    return

                typer.echo("error: pass --enable, --disable, or --show", err=True)
                raise typer.Exit(code=2)
        finally:
            await engine.dispose()

    _run(_do())


# --------------------------------------------------------------------------- #
# batch — operator commands for inspecting submitted batches (Phase 59)       #
# --------------------------------------------------------------------------- #


batch_app = typer.Typer(
    name="batch",
    help="Inspect and manage Pronaos batches (Phase 59).",
    no_args_is_help=True,
)


@batch_app.command("list")
def batch_list(
    team_id: Annotated[
        str | None,
        typer.Option("--team-id", help="Filter to this team."),
    ] = None,
    status: Annotated[
        str | None,
        typer.Option(
            "--status",
            help="Filter to this Pronaos-normalized status "
            "(validating | in_progress | finalizing | completed | "
            "failed | expired | cancelled).",
        ),
    ] = None,
    limit: Annotated[
        int,
        typer.Option("--limit", help="Maximum rows to show."),
    ] = 50,
) -> None:
    """List recent batches across (or within) a team.

    Output is tab-separated: ``id\\tteam_id\\tprovider\\tstatus\\t``
    ``completed/total\\tcost_hcents\\tcreated_at`` — pipe to ``awk`` or
    ``column -t`` for prettier display.
    """
    from pronaos.db.models import Batch

    async def _do() -> None:
        engine = create_engine(get_settings())
        sm = create_sessionmaker(engine)
        try:
            async with get_session(sm) as session:
                stmt = select(Batch).order_by(Batch.created_at.desc()).limit(limit)
                if team_id:
                    stmt = stmt.where(Batch.team_id == team_id)
                if status:
                    stmt = stmt.where(Batch.status == status)
                result = await session.execute(stmt)
                rows = result.scalars().all()
                if not rows:
                    typer.echo("(no batches matched)")
                    return
                for r in rows:
                    typer.echo(
                        f"{r.id}\t{r.team_id}\t{r.provider}\t{r.status}\t"
                        f"{r.completed_count}/{r.request_count}\t"
                        f"{r.cost_hcents}\t"
                        f"{r.created_at.isoformat()}"
                    )
        finally:
            await engine.dispose()

    _run(_do())


@batch_app.command("show")
def batch_show(batch_id: str) -> None:
    """Print a single batch row in detail."""
    from pronaos.db.models import Batch

    async def _do() -> None:
        engine = create_engine(get_settings())
        sm = create_sessionmaker(engine)
        try:
            async with get_session(sm) as session:
                row = await session.get(Batch, batch_id)
                if row is None:
                    typer.echo(f"batch not found: {batch_id}", err=True)
                    raise typer.Exit(code=1)
                typer.echo(f"id: {row.id}")
                typer.echo(f"tenant_id: {row.tenant_id}")
                typer.echo(f"team_id: {row.team_id}")
                typer.echo(f"key_id: {row.key_id}")
                typer.echo(f"provider: {row.provider}")
                typer.echo(f"provider_batch_id: {row.provider_batch_id}")
                typer.echo(f"status: {row.status}")
                typer.echo(f"endpoint: {row.endpoint}")
                typer.echo(f"completion_window: {row.completion_window}")
                typer.echo(
                    f"request_counts: {row.completed_count} completed / "
                    f"{row.failed_count} failed / {row.request_count} total"
                )
                typer.echo(f"tokens: prompt={row.prompt_tokens} completion={row.completion_tokens}")
                typer.echo(f"cost_hcents: {row.cost_hcents}")
                typer.echo(f"created_at: {row.created_at.isoformat()}")
                if row.in_progress_at:
                    typer.echo(f"in_progress_at: {row.in_progress_at.isoformat()}")
                if row.completed_at:
                    typer.echo(f"completed_at: {row.completed_at.isoformat()}")
                if row.error_message:
                    typer.echo(f"error_message: {row.error_message}")
        finally:
            await engine.dispose()

    _run(_do())


app.add_typer(batch_app, name="batch")


# --------------------------------------------------------------------------- #
# abtest — per-team A/B routing tests (Phase 29)                              #
# --------------------------------------------------------------------------- #


def _parse_arm(raw: str) -> tuple[str, float]:
    """Parse a ``<model>:<weight>`` arm specifier.

    Both forms are accepted: ``groq/llama-8b:0.5`` and
    ``groq/llama-8b:50`` (the second is normalised to 0.5). Weights
    must be ≥ 0 and not both be 0.
    """
    if ":" not in raw:
        raise typer.BadParameter(f"arm must be 'model:weight', got {raw!r}")
    model, _, w_str = raw.rpartition(":")
    if not model:
        raise typer.BadParameter(f"empty model in arm spec {raw!r}")
    try:
        w = float(w_str)
    except ValueError as e:
        raise typer.BadParameter(f"invalid weight in arm spec {raw!r}: {w_str}") from e
    if w < 0:
        raise typer.BadParameter(f"arm weight must be >= 0 (got {w})")
    # Normalise percentages > 1 to fractions for ergonomics.
    if w > 1.0:
        w = w / 100.0
    return model, w


@abtest_app.command("create")
def abtest_create(
    team_id: Annotated[str, typer.Option("--team", "-t", help="Team id to attach the test to.")],
    name: Annotated[str, typer.Option("--name", "-n", help="Human-friendly label.")],
    arm_a: Annotated[
        str,
        typer.Option(
            "--arm-a",
            help="First arm as ``provider/model:weight`` (e.g. anthropic/claude-3-5-haiku:0.5).",
        ),
    ],
    arm_b: Annotated[
        str,
        typer.Option(
            "--arm-b",
            help="Second arm as ``provider/model:weight``.",
        ),
    ],
) -> None:
    """Activate an A/B routing test on a team. At most one active test
    per team; running ``create`` while a test is already active replaces
    the active config (the old test's usage rows stay in place for
    later reporting via ``abtest report --test-id``).

    Examples
    --------
        pronaos-cli abtest create -t <team> -n haiku-vs-sonnet \\
            --arm-a anthropic/claude-3-5-haiku:0.5 \\
            --arm-b anthropic/claude-3-5-sonnet:0.5
    """
    from datetime import UTC, datetime

    a_model, a_weight = _parse_arm(arm_a)
    b_model, b_weight = _parse_arm(arm_b)
    if a_model == b_model:
        typer.echo(
            f"error: arm-a and arm-b cannot be the same model ({a_model})",
            err=True,
        )
        raise typer.Exit(code=2)
    if a_weight + b_weight <= 0:
        typer.echo("error: at least one arm weight must be > 0", err=True)
        raise typer.Exit(code=2)

    import uuid as _uuid

    test_id = _uuid.uuid4().hex
    payload = {
        "id": test_id,
        "name": name,
        "started_at": datetime.now(tz=UTC).isoformat(),
        "arm_a": {"model": a_model, "weight": a_weight},
        "arm_b": {"model": b_model, "weight": b_weight},
    }

    async def _do() -> None:
        engine = create_engine(get_settings())
        sm = create_sessionmaker(engine)
        try:
            async with get_session(sm) as session:
                team = await session.get(Team, team_id)
                if team is None:
                    typer.echo(f"team not found: {team_id}", err=True)
                    raise typer.Exit(code=1)
                team.ab_test = payload
                typer.echo(
                    f"ok\t{team_id}\ttest_id={test_id}\tname={name}\t"
                    f"a={a_model}:{a_weight:.3f}\tb={b_model}:{b_weight:.3f}"
                )
        finally:
            await engine.dispose()

    _run(_do())


@abtest_app.command("show")
def abtest_show(
    team_id: Annotated[str, typer.Option("--team", "-t", help="Team id.")],
) -> None:
    """Print the team's active A/B test config (or '(unset)')."""

    async def _do() -> None:
        engine = create_engine(get_settings())
        sm = create_sessionmaker(engine)
        try:
            async with get_session(sm) as session:
                team = await session.get(Team, team_id)
                if team is None:
                    typer.echo(f"team not found: {team_id}", err=True)
                    raise typer.Exit(code=1)
                if not team.ab_test:
                    typer.echo("(unset — no active A/B test)")
                    return
                import json as _json

                typer.echo(_json.dumps(team.ab_test, indent=2))
        finally:
            await engine.dispose()

    _run(_do())


@abtest_app.command("stop")
def abtest_stop(
    team_id: Annotated[str, typer.Option("--team", "-t", help="Team id.")],
) -> None:
    """Deactivate the team's A/B test. The team's ``ab_test`` column
    is cleared; previous usage rows remain in place (still tagged
    with their ``ab_arm`` letter) so a later ``report`` call can
    still aggregate the now-completed experiment.
    """

    async def _do() -> None:
        engine = create_engine(get_settings())
        sm = create_sessionmaker(engine)
        try:
            async with get_session(sm) as session:
                team = await session.get(Team, team_id)
                if team is None:
                    typer.echo(f"team not found: {team_id}", err=True)
                    raise typer.Exit(code=1)
                team.ab_test = None
                typer.echo(f"ok\t{team_id}\tab_test=stopped")
        finally:
            await engine.dispose()

    _run(_do())


@abtest_app.command("report")
def abtest_report(
    team_id: Annotated[str, typer.Option("--team", "-t", help="Team id to report on.")],
    since: Annotated[
        str | None,
        typer.Option(
            "--since",
            help="ISO-8601 lower bound for usage rows (default: test's started_at).",
        ),
    ] = None,
) -> None:
    """Aggregate per-arm stats from ``usage_records`` and run Welch's
    t-test on mean cost-per-call between arms.

    The team's active test (or the most recent stopped one, if no
    active test exists) defines the arm letters and started_at lower
    bound. Override with ``--since`` to scope to a different window.
    """
    from datetime import datetime

    from sqlalchemy import select as _select

    from pronaos.core.abtest_stats import summarise_arm, welchs_t_test
    from pronaos.db.models import UsageRecord as _UR

    async def _do() -> None:
        engine = create_engine(get_settings())
        sm = create_sessionmaker(engine)
        try:
            async with get_session(sm) as session:
                team = await session.get(Team, team_id)
                if team is None:
                    typer.echo(f"team not found: {team_id}", err=True)
                    raise typer.Exit(code=1)
                test_started: datetime | None = None
                if not team.ab_test:
                    typer.echo(
                        "(no active A/B test on team — pass --since to scope a stopped one)",
                        err=True,
                    )
                    if since is None:
                        raise typer.Exit(code=1)
                    test_name = "(stopped)"
                    test_id_str = "(none)"
                else:
                    test_name = team.ab_test.get("name", "(unnamed)")
                    test_id_str = team.ab_test.get("id", "(no-id)")
                    started_at_raw = team.ab_test.get("started_at")
                    if started_at_raw:
                        test_started = datetime.fromisoformat(started_at_raw)

                lower = datetime.fromisoformat(since) if since is not None else test_started

                stmt = _select(_UR).where(
                    _UR.team_id == team_id,
                    _UR.ab_arm.in_(["a", "b"]),
                )
                if lower is not None:
                    stmt = stmt.where(_UR.ts >= lower)
                rows = (await session.execute(stmt)).scalars().all()

                a_costs: list[int] = [r.cost_hcents for r in rows if r.ab_arm == "a"]
                b_costs: list[int] = [r.cost_hcents for r in rows if r.ab_arm == "b"]
                a_tokens: list[int] = [
                    r.prompt_tokens + r.completion_tokens for r in rows if r.ab_arm == "a"
                ]
                b_tokens: list[int] = [
                    r.prompt_tokens + r.completion_tokens for r in rows if r.ab_arm == "b"
                ]

                arm_a_stats = summarise_arm("a", a_costs, a_tokens)
                arm_b_stats = summarise_arm("b", b_costs, b_tokens)

                typer.echo(f"test:        {test_name} ({test_id_str})")
                typer.echo(f"team:        {team_id}")
                typer.echo(f"since:       {lower.isoformat() if lower else '(all time)'}")
                typer.echo("")
                typer.echo("                       arm a            arm b")
                typer.echo(f"  n samples         {arm_a_stats.n:>10d}       {arm_b_stats.n:>10d}")
                typer.echo(
                    f"  mean cost (hc)    {arm_a_stats.mean_cost_hcents:>10.3f}       "
                    f"{arm_b_stats.mean_cost_hcents:>10.3f}"
                )
                typer.echo(
                    f"  mean tokens       {arm_a_stats.mean_total_tokens:>10.1f}       "
                    f"{arm_b_stats.mean_total_tokens:>10.1f}"
                )
                typer.echo("")
                if arm_a_stats.n < 2 or arm_b_stats.n < 2:
                    typer.echo("(need >=2 samples per arm to run the t-test)")
                    return
                result = welchs_t_test([float(c) for c in a_costs], [float(c) for c in b_costs])
                if result is None:
                    typer.echo("(t-test undefined for these samples)")
                    return
                typer.echo("Welch's t-test on cost_hcents (a - b):")
                typer.echo(f"  t-statistic:   {result.t_statistic:.4f}")
                typer.echo(f"  df:            {result.df:.2f}")
                typer.echo(f"  p-value:       {result.p_value:.6g}")
                typer.echo(f"  95% CI (a-b):  [{result.ci_low:.3f}, {result.ci_high:.3f}] hcents")
                typer.echo(f"  Cohen's d:     {result.cohens_d:.3f}")
                verdict = (
                    "SIGNIFICANT (p<0.05)"
                    if result.significant_at_05
                    else "not significant at alpha=0.05"
                )
                typer.echo(f"  verdict:       {verdict}")
        finally:
            await engine.dispose()

    _run(_do())


def main() -> None:
    app()


if __name__ == "__main__":
    main()
