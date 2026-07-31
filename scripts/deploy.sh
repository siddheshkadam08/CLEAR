#!/usr/bin/env bash
#
# Bootstrap the stack on a server. Runs *on* the target host.
#
#   ./scripts/deploy.sh [--fresh]
#
# Idempotent: safe to re-run for an update. It never overwrites an existing .env,
# because that file holds the generated secrets and the seeded admin password -
# regenerating them mid-life would invalidate every issued token and lock people
# out of a running deployment.
#
#   --fresh   Also DELETE all volumes first. Destroys the database, the object
#             store and every uploaded contract. Prompts before doing it.
#
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m  %s\n' "$*"; }
die()  { printf '\033[1;31mxx\033[0m  %s\n' "$*" >&2; exit 1; }

FRESH=0
[[ "${1:-}" == "--fresh" ]] && FRESH=1

# -----------------------------------------------------------------------------
# Prerequisites
# -----------------------------------------------------------------------------
command -v podman >/dev/null 2>&1 || die "podman is not installed or not on PATH."

if command -v podman-compose >/dev/null 2>&1; then
  COMPOSE=(podman-compose)
elif podman compose version >/dev/null 2>&1; then
  COMPOSE=(podman compose)

  # `podman compose` delegates to the Docker Compose plugin, which talks to a
  # Docker-compatible socket rather than to podman directly. Rootless podman does
  # not run that socket by default, and the failure is opaque: the build gets most
  # of the way through and then reports "failed to connect to the docker API at
  # unix:///run/user/<uid>/podman/podman.sock".
  if [[ ! -S "/run/user/$(id -u)/podman/podman.sock" ]]; then
    log "Starting the rootless podman socket (needed by the compose plugin)."
    systemctl --user enable --now podman.socket >/dev/null 2>&1 \
      || warn "Could not enable podman.socket via systemd; the build may fail."
  fi

  # Without linger, the user's systemd session ends at logout and takes every
  # container with it - the stack would die the moment this SSH session closes.
  if ! loginctl show-user "$(whoami)" 2>/dev/null | grep -q 'Linger=yes'; then
    log "Enabling linger so containers keep running after logout."
    loginctl enable-linger "$(whoami)" 2>/dev/null \
      || warn "Could not enable linger. Containers will stop when you log out;
       ask an administrator to run: loginctl enable-linger $(whoami)"
  fi
else
  die "Neither 'podman-compose' nor 'podman compose' is available. Install one:
    pip3 install --user podman-compose"
fi
log "Using: ${COMPOSE[*]}"

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------
# Real secrets, generated once on first deploy. `openssl rand -hex 32` rather than
# a fixed string: the production guard in app/core/config.py refuses to boot on the
# shipped defaults, and a dev server on a public IP deserves the same treatment.
gen_secret() { openssl rand -hex 32 2>/dev/null || head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'; }

# Is anything already listening on this port, on any interface?
port_taken() {
  local port="$1"
  if command -v ss >/dev/null 2>&1; then
    ss -lnt 2>/dev/null | awk '{print $4}' | grep -qE "[:.]${port}\$"
  else
    netstat -lnt 2>/dev/null | awk '{print $4}' | grep -qE "[:.]${port}\$"
  fi
}

# First free port at or above the preferred one.
#
# A shared dev box usually has something on the obvious ports already - this host
# was running a MinIO on 9000/9001 before this stack arrived. Walking to the next
# free port is better than either failing the deploy or, far worse, stopping
# whatever was there first.
free_port() {
  local port="$1" limit=$(( $1 + 40 ))
  while port_taken "$port" && [[ $port -lt $limit ]]; do
    port=$(( port + 1 ))
  done
  printf '%s' "$port"
}

if [[ -f .env ]]; then
  log ".env already exists - keeping it (secrets and admin password preserved)."
else
  [[ -f .env.example ]] || die ".env.example is missing; cannot generate .env."
  log "Generating .env with fresh secrets."

  # The address a browser will use. Prefer an explicit PUBLIC_HOST: on a cloud VM
  # `hostname -I` returns the private address, which nobody outside the VPC can
  # reach, and every presigned download URL would be signed for it.
  PUBLIC_HOST="${PUBLIC_HOST:-$(hostname -I 2>/dev/null | awk '{print $1}')}"
  PUBLIC_HOST="${PUBLIC_HOST:-127.0.0.1}"

  FRONTEND_PORT="$(free_port "${FRONTEND_PORT:-8080}")"
  BACKEND_PORT="$(free_port "${BACKEND_PORT:-8000}")"
  MINIO_PORT="$(free_port "${MINIO_PORT:-9000}")"
  MINIO_CONSOLE_PORT="$(free_port "$(( MINIO_PORT + 1 ))")"
  PG_PORT="$(free_port "${PG_PORT:-5432}")"
  RD_PORT="$(free_port "${RD_PORT:-6379}")"
  Q_PORT="$(free_port "${Q_PORT:-9100}")"
  log "Ports: frontend=${FRONTEND_PORT} api=${BACKEND_PORT} minio=${MINIO_PORT} (console ${MINIO_CONSOLE_PORT}) postgres=${PG_PORT} redis=${RD_PORT} queue=${Q_PORT}"

  cp .env.example .env

  set_var() {
    local key="$1" value="$2"
    if grep -qE "^${key}=" .env; then
      # `|` as the delimiter: values contain URLs full of slashes.
      sed -i "s|^${key}=.*|${key}=${value}|" .env
    else
      printf '%s=%s\n' "$key" "$value" >> .env
    fi
  }

  set_var APP_ENV staging
  set_var DEBUG false
  set_var LOG_LEVEL INFO

  set_var JWT_SECRET "$(gen_secret)"
  set_var INTERNAL_API_TOKEN "$(gen_secret)"
  set_var POSTGRES_PASSWORD "$(gen_secret)"
  # MinIO reads these through compose as MINIO_ROOT_USER / MINIO_ROOT_PASSWORD;
  # the app and the bucket-creation job read the same two variables, so one value
  # each is all that is needed.
  set_var S3_ACCESS_KEY_ID "cip-$(head -c 6 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  set_var S3_SECRET_ACCESS_KEY "$(gen_secret)"

  # DATABASE_URL is only regenerated when it targets the bundled container - in
  # that case it embeds the password just generated above and has to match.
  #
  # When .env.example points at an external database it is kept, with one
  # rewrite: this VM cannot reach the shared server's *public* address (there is
  # no hairpin route back to a peer's public IP inside the VPC), only its private
  # one. Blindly overwriting the URL here is how a deployment silently ends up on
  # a throwaway container while everyone believes it is on the shared database.
  if grep -qE '^DATABASE_URL=.*@postgres:5432/' .env; then
    DB_PASS="$(grep -E '^POSTGRES_PASSWORD=' .env | cut -d= -f2-)"
    set_var DATABASE_URL "postgresql+asyncpg://cip:${DB_PASS}@postgres:5432/cip"
  else
    sed -i 's|@35\.154\.17\.203:|@172.15.151.102:|' .env
    log "External DATABASE_URL kept (public address rewritten to the private one)."
  fi

  # Infrastructure binds to loopback only. This host has a public IP; publishing
  # Postgres, Redis or the MinIO console on 0.0.0.0 would put them on the open
  # internet, which is how dev databases end up in breach reports.
  #
  # MinIO's S3 port is the one exception: the PDF viewer fetches presigned URLs
  # directly from the browser, so that port must be reachable or every document
  # download 404s. Its console stays on loopback.
  set_var POSTGRES_PORT "127.0.0.1:${PG_PORT}"
  set_var REDIS_PORT "127.0.0.1:${RD_PORT}"
  set_var MINIO_CONSOLE_PORT "127.0.0.1:${MINIO_CONSOLE_PORT}"
  set_var QUEUE_PORT "127.0.0.1:${Q_PORT}"

  set_var FRONTEND_PORT "$FRONTEND_PORT"
  set_var BACKEND_PORT "$BACKEND_PORT"
  set_var MINIO_PORT "$MINIO_PORT"

  # The runtime frontend image is nginx serving on 8080 and proxying /api to the
  # backend. The dev override runs Vite on 5173 instead, which is what
  # .env.example is written for - leaving that value here would publish the host
  # port against a container port nothing is listening on.
  set_var FRONTEND_TARGET_PORT 8080

  # Forced to the same-origin path, NOT left to .env.example.
  #
  # This used to be a no-op on the belief that .env.example already defaulted to
  # `/api/v1`. It does not - it ships `http://localhost:8000/api/v1`, which is
  # correct for local development (Vite on 5173 calling the API on 8000) and
  # catastrophic once deployed: the value is baked into the bundle at build time,
  # so every browser then calls *its own* localhost:8000. Nothing is listening
  # there, so every request fails and **login is impossible** - with no
  # server-side error to show for it, because the request never arrives.
  #
  # Same-origin is also the correct security posture: nginx proxies /api to the
  # backend, so the HttpOnly refresh cookie stays first-party rather than
  # depending on SameSite=None plus CORS credentials.
  set_var VITE_API_BASE_URL /api/v1

  # The bucket name doubles as the nginx location that proxies object storage, so
  # it must not collide with a route the SPA owns - `contracts` would shadow
  # /contracts/<uuid>.
  set_var STORAGE_CONTAINER cip-documents

  # The browser resolves this itself, so it must be an address a browser can
  # actually reach. It points at the frontend rather than at MinIO's own port:
  # nginx proxies the bucket path, which keeps the deployment to one public port
  # and works without a firewall change. A presigned URL pointing at
  # http://minio:9000 - or at a port the security group blocks - is not fetchable
  # from anyone's laptop, and every document download 404s or times out.
  set_var CORS_ORIGINS "http://${PUBLIC_HOST}:${FRONTEND_PORT}"
  set_var S3_PUBLIC_ENDPOINT_URL "http://${PUBLIC_HOST}:${FRONTEND_PORT}"

  # MinIO's own port stays on loopback now that nginx fronts it.
  set_var MINIO_PORT "127.0.0.1:${MINIO_PORT}"

  chmod 600 .env
  log "Wrote .env (mode 600). Secrets are generated and unique to this host."
fi

# The dev override swaps in Vite's dev server and mounts source from the repo;
# neither is wanted on a server. Compose picks it up automatically if present.
if [[ -f docker-compose.override.yml ]]; then
  warn "Moving docker-compose.override.yml aside - it is a local-development file."
  mv docker-compose.override.yml docker-compose.override.yml.local
fi

# -----------------------------------------------------------------------------
# Preflight: refuse to deploy a simulation
#
# The mock providers and the fixture parser exist so tests and offline work are
# possible without a key, and .env.example carries them as the *local* default
# for exactly that reason. Deployed, they are indistinguishable from the real
# thing at a glance: the pipeline runs green, contracts reach READY, and every
# clause, date and party in the output was synthesised rather than read from the
# document. Nothing downstream can tell, so it has to be caught here.
#
# Set ALLOW_SIMULATED_AI=1 to deploy one deliberately (a UI demo with no keys).
# -----------------------------------------------------------------------------
env_value() { grep -E "^$1=" .env 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"'; }

SIMULATED=()
[[ "$(env_value LLM_PROVIDER)"       == "mock"    ]] && SIMULATED+=("LLM_PROVIDER=mock")
[[ "$(env_value EMBEDDING_PROVIDER)" == "mock"    ]] && SIMULATED+=("EMBEDDING_PROVIDER=mock")
[[ "$(env_value ACTIVE_PARSER)"      == "mock"    ]] && SIMULATED+=("ACTIVE_PARSER=mock")
[[ "$(env_value PARSER_MODE)"        == "fixture" ]] && SIMULATED+=("PARSER_MODE=fixture")

if [[ ${#SIMULATED[@]} -gt 0 ]]; then
  if [[ "${ALLOW_SIMULATED_AI:-0}" == "1" ]]; then
    warn "Deploying with simulated AI: ${SIMULATED[*]}"
    warn "Extractions will be SYNTHESISED, not read from the uploaded documents."
  else
    warn "This .env would deploy a simulation, not the product:"
    for entry in "${SIMULATED[@]}"; do warn "    ${entry}"; done
    warn ""
    warn "Set the real providers in .env on this host, then re-run:"
    warn "    LLM_PROVIDER=anthropic|openai|gemini  + the matching API key"
    warn "    EMBEDDING_PROVIDER=nvidia|openai      + the matching API key"
    warn "    PARSER_MODE=live                      + IDOC_BASE_URL / IDOC_API_KEY"
    warn ""
    die "Refusing to deploy simulated AI. Re-run with ALLOW_SIMULATED_AI=1 to override."
  fi
fi

# -----------------------------------------------------------------------------
# Bring the stack up
# -----------------------------------------------------------------------------
if [[ $FRESH -eq 1 ]]; then
  warn "--fresh will DELETE all volumes: database, object store, every contract."
  read -r -p "Type 'destroy' to confirm: " reply
  [[ "$reply" == "destroy" ]] || die "Aborted."
  "${COMPOSE[@]}" down -v || true
fi

log "Building images (this takes a few minutes on a first run)."
"${COMPOSE[@]}" build

log "Starting services."
"${COMPOSE[@]}" up -d

log "Waiting for the API to report healthy."
BACKEND_PORT="$(grep -E '^BACKEND_PORT=' .env | cut -d= -f2- | tr -d '"' | awk -F: '{print $NF}')"
BACKEND_PORT="${BACKEND_PORT:-8000}"

for attempt in $(seq 1 60); do
  # `/healthz` is the liveness probe; `/readyz` additionally checks the database
  # and would stay red until migrations have run, which happens further down.
  if curl -fsS "http://127.0.0.1:${BACKEND_PORT}/healthz" >/dev/null 2>&1; then
    log "API is healthy."
    break
  fi
  [[ $attempt -eq 60 ]] && {
    warn "The API did not become healthy within 5 minutes. Recent logs:"
    "${COMPOSE[@]}" logs --tail 60 backend || true
    die "Deployment failed."
  }
  sleep 5
done

log "Applying migrations."
"${COMPOSE[@]}" exec -T backend alembic upgrade head

log "Seeding roles, admin, clause master and profiles."
"${COMPOSE[@]}" exec -T backend python -m app.cli seed

# -----------------------------------------------------------------------------
# Report
# -----------------------------------------------------------------------------
PUBLIC_HOST="$(grep -E '^S3_PUBLIC_ENDPOINT_URL=' .env | sed -E 's|.*//([^:]+):.*|\1|')"
FRONTEND_PORT="$(grep -E '^FRONTEND_PORT=' .env | cut -d= -f2- | tr -d '"' | awk -F: '{print $NF}')"
ADMIN_EMAIL="$(grep -E '^SEED_ADMIN_EMAIL=' .env | cut -d= -f2-)"

cat <<EOF

$(log "Deployed.")

  Frontend   http://${PUBLIC_HOST}:${FRONTEND_PORT}
  API docs   http://${PUBLIC_HOST}:${BACKEND_PORT}/docs
  Sign in    ${ADMIN_EMAIL}  (password: see SEED_ADMIN_PASSWORD in .env)

  Postgres, Redis, the MinIO console and the queue admin port are bound to
  127.0.0.1 and are not reachable from outside this host. Reach them over an SSH
  tunnel if you need to.

  Change the seeded admin password after first sign-in.

EOF

"${COMPOSE[@]}" ps
