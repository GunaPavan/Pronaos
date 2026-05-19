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
app.add_typer(tenant_app, name="tenant")
app.add_typer(team_app, name="team")
app.add_typer(key_app, name="key")
app.add_typer(db_app, name="db")
app.add_typer(eval_app, name="eval")
app.add_typer(audit_app, name="audit")


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
                display = (
                    "unlimited"
                    if hcents is None
                    else f"${hcents / 10_000:,.2f}"
                )
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
                tb_pct = (
                    f" ({100 * tokens_used / token_budget:.1f}%)" if token_budget else ""
                )
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
    group_by: Annotated[
        str, typer.Option(help="provider | model | status")
    ] = "model",
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
                typer.echo(f"tokens:   {total_tokens:,}  ({int(prompt_sum):,} in / {int(completion_sum):,} out)")  # noqa: E501 — single-line summary for terminal readability
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
                    f"  {group_by:<{name_width}}  {'requests':>8}  "
                    f"{'tokens':>12}  {'cost':>12}"
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
            help="Model id used as the judge (e.g. anthropic/claude-haiku-4-5). "
            "Choose a stronger model than the candidate when you can.",
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
    base_url: Annotated[
        str, typer.Option(help="Gateway base URL.")
    ] = "http://localhost:8080",
    pass_threshold: Annotated[
        float,
        typer.Option(
            "--threshold",
            help="A case 'passes' when its score >= this (0.0-1.0).",
        ),
    ] = 0.7,
    output: Annotated[
        Path | None,
        typer.Option(
            "--output", "-o", help="Save the full result JSON to this path."
        ),
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
    from pronaos.eval.runner import EvalRunner
    from pronaos.eval.scorer import LLMJudgeScorer

    try:
        gs = load_golden_set(golden_set)
    except (FileNotFoundError, ValueError) as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(code=2) from None

    scorer = LLMJudgeScorer(
        base_url=base_url, api_key=api_key, judge_model=judge_model
    )
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
    typer.echo(
        f"{'case':<28} {'cat':<14} {'score':>6}  reason"
    )
    typer.echo("-" * 80)
    for row in summary.rows:
        if row.candidate_error:
            typer.echo(f"{row.case_id:<28} {row.category:<14} {'ERR':>6}  {row.candidate_error[:36]}")  # noqa: E501 — fixed-width display row
        elif row.judge_error:
            typer.echo(f"{row.case_id:<28} {row.category:<14} {'judge?':>6}  {row.judge_error[:36]}")  # noqa: E501
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
            typer.echo(
                f"  {c.name:<20} {c.count:>4} {c.mean:>6.2f} {c.pass_rate:>5.0%}"
            )

    if output is not None:
        summary.save_json(output)
        typer.echo("")
        typer.echo(f"saved: {output}")


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
            help="Add a rule to the disabled list (skip it entirely). "
            "Repeatable.",
        ),
    ] = [],  # noqa: B006 — Typer interprets [] as 'no occurrences'
    enable: Annotated[
        list[str],
        typer.Option(
            "--enable",
            help="Remove a rule from the disabled list (return to engine default). "
            "Repeatable.",
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
                    typer.echo(
                        f"ok\t{id}\tallowed_models=" + ",".join(patterns)
                    )
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
                    typer.echo(
                        f"  - record {b.record_id} @ {b.ts_iso}: {b.reason}"
                    )
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

            typer.echo(
                f"{'ts':<26} {'model':<38} {'this_hash':<18} prev_hash"
            )
            for r in rows:
                this_short = r.this_hash[:16] + ".."
                prev_short = (r.prev_hash[:16] + "..") if r.prev_hash else "(genesis)"
                typer.echo(
                    f"{r.ts.isoformat()[:26]:<26} {r.model:<38} "
                    f"{this_short:<18} {prev_short}"
                )
        finally:
            await engine.dispose()

    _run(_do())


def main() -> None:
    app()


if __name__ == "__main__":
    main()
