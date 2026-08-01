<#
.SYNOPSIS
    Point a local backend at the podman dev stack and run it.

.DESCRIPTION
    Sets the handful of values that differ between a container run and a native
    one, then starts the API. Everything else still comes from `.env`.

    These are set as *process* environment variables rather than written into
    `.env`, and that is deliberate: `.env` holds real credentials and points at
    the shared Hackathon database. Overriding in the shell means a local run
    cannot accidentally be committed, and cannot leave the file pointing
    somewhere the next person did not expect.

    pydantic-settings reads the real environment before the dotenv file, so
    anything set here wins.

.EXAMPLE
    .\scripts\dev-local.ps1 -Migrate -Seed
    .\scripts\dev-local.ps1 -Serve
#>
[CmdletBinding()]
param(
    [switch]$Migrate,
    [switch]$Seed,
    [switch]$Serve,
    [switch]$EnableSso,
    # 8000 and 5173 are already taken on this machine - 8000 by a separate
    # "Contract Intelligence POC" and 5173 by the `cipdemo-frontend` container -
    # so the defaults here step around both rather than fighting them.
    #
    # If you change ApiPort you must also add the matching redirect URI to the
    # Entra app registration. Entra compares it byte for byte, port included.
    [int]$ApiPort = 8010,
    [int]$WebPort = 5174,
    # The container ports the demo stack publishes. Not the defaults: 5432 and
    # 6379 are frequently already taken on a developer machine, and the stack
    # was brought up on high ports to avoid the collision.
    [int]$PostgresPort = 55432,
    [int]$RedisPort = 56379,
    [int]$MinioPort = 59000
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$backend = Join-Path $repo 'backend'
$python = Join-Path $backend '.venv\Scripts\python.exe'

if (-not (Test-Path $python)) {
    throw "No virtualenv at $python. Run: cd backend; python -m venv .venv; .\.venv\Scripts\pip install -e '.[dev,ai]'"
}

# --- point at the local stack -------------------------------------------------
# The async driver for the app; Alembic swaps in the sync one itself.
$env:DATABASE_URL = "postgresql+asyncpg://cip:cip_dev_password@localhost:$PostgresPort/cip"
$env:REDIS_URL = "redis://localhost:$RedisPort/0"
$env:STORAGE_ENDPOINT_URL = "http://localhost:$MinioPort"
$env:STORAGE_PUBLIC_ENDPOINT_URL = "http://localhost:$MinioPort"

# Tracing off by default: without a collector the exporter retries on every
# request and makes the logs unreadable for no benefit.
if (-not $env:OTEL_ENABLED) { $env:OTEL_ENABLED = 'false' }

# --- Microsoft SSO ------------------------------------------------------------
$env:CORS_ORIGINS = "http://localhost:$WebPort,http://localhost:5173,http://localhost:3000"

if ($EnableSso) {
    $env:OIDC_ENABLED = 'true'
    # Directory (tenant) ID, then Application (client) ID. The original snippet
    # had these the other way round - `f72edf57` was in the URL path and
    # `06e84b96` in `client_id`. Swapped is not a subtle failure: Entra answers
    # AADSTS900023 ("Specified tenant identifier is neither a valid DNS name nor
    # a valid external domain") because it cannot find a directory by that id.
    $env:AZURE_AD_TENANT_ID = 'f72edf57-01e0-4138-aca3-de022cfc0ca2'
    $env:AZURE_AD_CLIENT_ID = '06e84b96-907a-4418-ae29-211bfd190e84'
    # No client secret: a SPA registration is a public client and PKCE
    # authenticates the code exchange instead.
    $env:OIDC_REDIRECT_URI = "http://localhost:$ApiPort/api/v1/auth/oidc/callback"
    $env:OIDC_POST_LOGIN_REDIRECT = "http://localhost:$WebPort/auth/callback"
    Write-Host "  SSO on. Register this redirect URI in Entra, exactly:" -ForegroundColor Cyan
    Write-Host "    $($env:OIDC_REDIRECT_URI)" -ForegroundColor Yellow
}

Push-Location $backend
try {
    if ($Migrate) {
        Write-Host "`n== Migrating ==" -ForegroundColor Cyan
        & $python -m alembic upgrade head
        if ($LASTEXITCODE -ne 0) { throw "Migration failed." }
    }

    if ($Seed) {
        Write-Host "`n== Seeding ==" -ForegroundColor Cyan
        & $python -m app.cli seed
        if ($LASTEXITCODE -ne 0) { throw "Seed failed." }
    }

    if ($Serve) {
        $busy = (Get-NetTCPConnection -LocalPort $ApiPort -State Listen -ErrorAction SilentlyContinue | Measure-Object).Count
        if ($busy) {
            # Uvicorn's own bind failure scrolls past in a detached window and the
            # next thing you notice is a 404 from whatever *is* on the port.
            throw "Port $ApiPort is already in use. Pass -ApiPort <free port>."
        }
        Write-Host "`n== API on http://localhost:$ApiPort ==" -ForegroundColor Green
        Write-Host "   docs  http://localhost:$ApiPort/docs"
        & $python -m uvicorn app.main:app --host 0.0.0.0 --port $ApiPort --reload
    }
}
finally {
    Pop-Location
}
