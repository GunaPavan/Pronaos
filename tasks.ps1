# Pronaos -- Windows / PowerShell task runner.
#
# Usage:
#   .\tasks.ps1                  # show help
#   .\tasks.ps1 <task> [args]    # run a task
#
# Mirrors the Unix Makefile so Windows contributors get the same ergonomics
# without needing 'make'. Sets UTF-8 mode globally so the CLI's box-drawing
# characters render on the default Windows console codepage.

$ErrorActionPreference = "Stop"

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8       = "1"

$VENV = ".\.venv\Scripts"
$PY   = "$VENV\python.exe"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
function NeedVenv {
    if (-not (Test-Path $PY)) {
        Write-Host "venv missing at $VENV. Run: .\tasks.ps1 install" -ForegroundColor Red
        exit 1
    }
}

function PrintHelp {
    Write-Host ""
    Write-Host "Pronaos task runner -- common developer tasks" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "  Setup:"
    Write-Host "    install            Create .venv and install package + dev deps"
    Write-Host ""
    Write-Host "  Run:"
    Write-Host "    dev                Run the gateway locally with reload"
    Write-Host "    cli [args]         Run pronaos-cli (UTF-8 mode set)"
    Write-Host ""
    Write-Host "  Quality:"
    Write-Host "    test               Run unit tests"
    Write-Host "    test-cov           Run unit tests with coverage report"
    Write-Host "    test-int           Run live integration tests (needs API keys)"
    Write-Host "    lint               Ruff lint"
    Write-Host "    fmt                Ruff format + autofix"
    Write-Host "    typecheck          Mypy strict"
    Write-Host "    ci                 lint + typecheck + test (everything CI runs)"
    Write-Host ""
    Write-Host "  Database:"
    Write-Host "    db-reset           Drop pronaos.db and re-run migrations"
    Write-Host "    db-upgrade         Run Alembic migrations to head"
    Write-Host ""
    Write-Host "  Maintenance:"
    Write-Host "    clean              Remove caches and build artifacts"
    Write-Host ""
    Write-Host "  End-to-end demo (proves auth + quotas work against real Groq):"
    Write-Host "    demo               Run the end-to-end demonstration"
    Write-Host ""
}

# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------
function TaskInstall {
    if (-not (Test-Path .\.venv)) {
        py -3.12 -m venv .venv
    }
    & $PY -m pip install --upgrade pip
    & $PY -m pip install -e ".[dev]"
}

function TaskDev {
    NeedVenv
    & $PY -m uvicorn pronaos.main:app --reload --host 0.0.0.0 --port 8080
}

function TaskCli {
    NeedVenv
    & $PY -m pronaos.cli @args
}

function TaskTest {
    NeedVenv
    & $PY -m pytest tests/unit -v
}

function TaskTestCov {
    NeedVenv
    & $PY -m pytest tests/unit --cov --cov-report=term-missing
}

function TaskTestInt {
    NeedVenv
    & $PY -m pytest -m integration tests/integration -v
}

function TaskLint {
    NeedVenv
    & $PY -m ruff check src tests
}

function TaskFmt {
    NeedVenv
    & $PY -m ruff check --fix src tests
    & $PY -m ruff format src tests
}

function TaskTypecheck {
    NeedVenv
    & $PY -m mypy src
}

function TaskCi {
    TaskLint
    if ($LASTEXITCODE -ne 0) { exit 1 }
    & $PY -m ruff format --check src tests
    if ($LASTEXITCODE -ne 0) { exit 1 }
    TaskTypecheck
    if ($LASTEXITCODE -ne 0) { exit 1 }
    TaskTest
}

function TaskDbReset {
    NeedVenv
    Remove-Item -ErrorAction Ignore pronaos.db, "pronaos.db-journal", "pronaos.db-wal", "pronaos.db-shm"
    & $PY -m pronaos.cli db upgrade
}

function TaskDbUpgrade {
    NeedVenv
    & $PY -m pronaos.cli db upgrade
}

function TaskClean {
    Remove-Item -Recurse -Force -ErrorAction Ignore `
        .mypy_cache, .pytest_cache, .ruff_cache, .coverage, htmlcov, coverage.xml, dist, build, demo_state.txt
    Get-ChildItem -Recurse -Directory -Force -ErrorAction Ignore -Filter __pycache__ | Remove-Item -Recurse -Force -ErrorAction Ignore
    Write-Host "Cleaned caches and build artifacts."
}

function TaskDemo {
    NeedVenv

    # Fresh DB and admin chain
    Remove-Item -ErrorAction Ignore pronaos.db, demo_state.txt
    & $PY -m pronaos.cli db upgrade | Out-Null
    Write-Host "[1/5] DB migrated" -ForegroundColor Green

    $tenantOut = @(& $PY -m pronaos.cli tenant create acme)
    $tenantId  = ($tenantOut[0] -split "`t")[0]
    Write-Host "[2/5] Tenant: $tenantId" -ForegroundColor Green

    $teamOut = @(& $PY -m pronaos.cli team create engineering --tenant $tenantId)
    $teamId  = ($teamOut[0] -split "`t")[0]
    Write-Host "[3/5] Team:   $teamId" -ForegroundColor Green

    $keyOut = @(& $PY -m pronaos.cli key issue --team $teamId --label "demo")
    $apiKey = @($keyOut | Select-String -Pattern "pn_live_\S+" | ForEach-Object { $_.Matches[0].Value })[0]
    $keyId  = @($keyOut | Select-String -Pattern "id:\s+([a-f0-9]+)" | ForEach-Object { $_.Matches[0].Groups[1].Value })[0]
    & $PY -m pronaos.cli key set-rps $keyId --rps 2           | Out-Null
    & $PY -m pronaos.cli team set-budget $teamId --tokens 200 | Out-Null
    Write-Host "[4/5] Key issued + quota set (rps=2, budget=200)" -ForegroundColor Green

    "$tenantId`n$teamId`n$keyId`n$apiKey" | Out-File -Encoding utf8 demo_state.txt
    Write-Host "[5/5] Demo state saved to demo_state.txt." -ForegroundColor Green
    Write-Host ""
    Write-Host "Now in a separate terminal, start the gateway: .\tasks.ps1 dev"
    $preview = $apiKey.Substring(0, [Math]::Min(20, $apiKey.Length))
    Write-Host "Then hit /v1/chat/completions with Authorization: Bearer $preview..."
}

# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------
$task = $args[0]
$rest = $args[1..($args.Length - 1)]

# Empty/null/help all go to help. Keep them separate for readability but use
# `break` so PowerShell's switch doesn't fall through to multiple matching arms.
if (-not $task -or $task -eq "help") {
    PrintHelp
    return
}

switch ($task) {
    "install"     { TaskInstall;    break }
    "dev"         { TaskDev;        break }
    "cli"         { $args = $rest; TaskCli; break }
    "test"        { TaskTest;       break }
    "test-cov"    { TaskTestCov;    break }
    "test-int"    { TaskTestInt;    break }
    "lint"        { TaskLint;       break }
    "fmt"         { TaskFmt;        break }
    "typecheck"   { TaskTypecheck;  break }
    "ci"          { TaskCi;         break }
    "db-reset"    { TaskDbReset;    break }
    "db-upgrade"  { TaskDbUpgrade;  break }
    "clean"       { TaskClean;      break }
    "demo"        { TaskDemo;       break }
    default {
        Write-Host "Unknown task: $task" -ForegroundColor Red
        PrintHelp
        exit 1
    }
}
