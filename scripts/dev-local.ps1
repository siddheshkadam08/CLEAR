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
    # Use the throwaway podman Postgres instead of the shared Hackathon DB.
    # Off by default: the point of a local run is usually to see the same data
    # the deployed app sees.
    [switch]$LocalStack,
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

# --- load .env into the process environment -----------------------------------
#
# Required, not a convenience. The nested settings groups in app/core/config.py
# (DatabaseSettings, LLMSettings, ...) are separate BaseSettings classes built by
# default_factory, and none of them declares `env_file` - so the parent's does
# not cascade and they read os.environ only.
#
# The failure is silent and expensive: a native run without this connects as
# user `cip` to localhost and falls back to mock AI providers, reporting nothing
# amiss. Compose injects the environment as real variables, which is why this
# only bites outside a container.
$envFile = Join-Path $repo '.env'
if (Test-Path $envFile) {
    $loaded = 0
    foreach ($line in Get-Content $envFile) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith('#')) { continue }
        $split = $trimmed.IndexOf('=')
        if ($split -lt 1) { continue }
        $key = $trimmed.Substring(0, $split).Trim()
        $value = $trimmed.Substring($split + 1).Trim()
        # Strip surrounding quotes, then an unquoted trailing comment. Doing it
        # in that order matters: a quoted value may legitimately contain a `#`.
        if ($value -match '^"(.*)"$' -or $value -match "^'(.*)'$") {
            $value = $Matches[1]
        }
        elseif ($value -match '\s+#') {
            $value = ($value -split '\s+#')[0].Trim()
        }
        Set-Item -Path "env:$key" -Value $value
        $loaded++
    }
    Write-Host "  loaded $loaded values from .env" -ForegroundColor DarkGray
}
else {
    Write-Warning "No .env at $envFile - the app will fall back to its defaults."
}

# --- override only what differs for a native run ------------------------------
if ($LocalStack) {
    # The podman demo stack, for working offline or against throwaway data.
    $env:DATABASE_URL = "postgresql+asyncpg://cip:cip_dev_password@localhost:$PostgresPort/cip"
    $env:REDIS_URL = "redis://localhost:$RedisPort/0"
    $env:STORAGE_ENDPOINT_URL = "http://localhost:$MinioPort"
    $env:STORAGE_PUBLIC_ENDPOINT_URL = "http://localhost:$MinioPort"
    Write-Host "  DB -> local podman stack (localhost:$PostgresPort)" -ForegroundColor Cyan
}
else {
    # .env already points DATABASE_URL at Hackathon-DB-SRV; only the service
    # hostnames need rewriting, because `redis` and `minio` resolve inside the
    # compose network and nowhere else.
    $env:REDIS_URL = "redis://localhost:$RedisPort/0"
    $env:STORAGE_ENDPOINT_URL = "http://localhost:$MinioPort"
    $env:STORAGE_PUBLIC_ENDPOINT_URL = "http://localhost:$MinioPort"
    $target = ($env:DATABASE_URL -replace '(://[^:]+:)[^@]+@', '$1***@')
    Write-Host "  DB -> $target" -ForegroundColor Cyan
}

# Tracing off by default: without a collector the exporter retries on every
# request and makes the logs unreadable for no benefit.
if (-not $env:OTEL_ENABLED) { $env:OTEL_ENABLED = 'false' }

# --- Microsoft SSO ------------------------------------------------------------
$env:CORS_ORIGINS = "http://localhost:$WebPort,http://localhost:5173,http://localhost:3000"

if ($EnableSso) {
    $env:OIDC_ENABLED = 'true'
    # Directory (tenant) ID, then Application (client) ID. Both are GUIDs and
    # transposing them is easy - Entra then answers AADSTS90002, "Tenant '<guid>'
    # not found", at the authorize step. The preflight below is the only way to
    # tell them apart without a browser round trip: a directory answers the
    # discovery endpoint and an application does not.
    $env:AZURE_AD_TENANT_ID = '06e84b96-907a-4418-ae29-211bfd190e84'
    $env:AZURE_AD_CLIENT_ID = 'f72edf57-01e0-4138-aca3-de022cfc0ca2'
    # No client secret: a SPA registration is a public client and PKCE
    # authenticates the code exchange instead.
    $env:OIDC_REDIRECT_URI = "http://localhost:$ApiPort/api/v1/auth/oidc/callback"
    $env:OIDC_POST_LOGIN_REDIRECT = "http://localhost:$WebPort/auth/callback"

    # Preflight: only a real directory serves a discovery document, so this
    # catches a tenant/client transposition before the first sign-in attempt
    # instead of after it. A failure here is a warning, not a throw - the machine
    # may simply be offline, and the rest of the app does not need Entra.
    $discovery = "https://login.microsoftonline.com/$($env:AZURE_AD_TENANT_ID)/v2.0/.well-known/openid-configuration"
    try {
        $null = Invoke-RestMethod $discovery -TimeoutSec 10
        Write-Host "  tenant $($env:AZURE_AD_TENANT_ID) resolves" -ForegroundColor DarkGray
    }
    catch {
        $detail = $_.ErrorDetails.Message
        if ($detail -match 'AADSTS\d+[^"]*') {
            Write-Warning "AZURE_AD_TENANT_ID is not a directory: $($Matches[0])"
            Write-Warning "Tenant and client are probably the wrong way round - sign-in will fail."
        }
        else {
            Write-Host "  could not reach Entra to check the tenant (offline?)" -ForegroundColor DarkGray
        }
    }

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
