#!/usr/bin/env bash
#
# Start the whole stack, in dependency order, and wait for it to be healthy.
#
#   ./start.sh
#
# Ordering is declared in the units themselves (After=/Requires=), so systemd
# would honour it from `systemctl --user start clear-frontend.service` alone.
# Starting them explicitly, one at a time, is for the operator: a failure names
# the service that failed instead of a dependency chain that gave up.

. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

load_images_env
require_podman
require_user_systemd

# ---------------------------------------------------------------------------
# Preflight - things that fail here are cheap; the same things fail expensively
# once half the stack is up.
# ---------------------------------------------------------------------------
[[ -f "$ENV_FILE" ]] || die "$ENV_FILE does not exist."

envget() { sed -n "s/^$1=//p" "$ENV_FILE" | tail -n 1; }

log "Preflight"

# A CRLF here corrupts every value in the file, invisibly: podman keeps the \r as
# part of the value, so DATABASE_URL points at a host with a carriage return in
# its name and the error names the host.
if grep -q $'\r' "$ENV_FILE"; then
  die "$ENV_FILE has CRLF line endings. Every value would carry a trailing carriage
return. Fix it:  sed -i 's/\r$//' $ENV_FILE"
fi

# Quoted values are a podman --env-file trap: the quotes become part of the value.
if grep -qE '^[A-Z_][A-Z0-9_]*=["'"'"']' "$ENV_FILE"; then
  grep -nE '^[A-Z_][A-Z0-9_]*=["'"'"']' "$ENV_FILE" >&2
  die "Quoted values in $ENV_FILE. podman --env-file keeps the quotes as part of
the value; CORS_ORIGINS=\"https://x\" never matches an origin. Remove them."
fi

for key in DATABASE_URL JWT_SECRET INTERNAL_API_TOKEN SEED_ADMIN_PASSWORD \
           NEW_USER_DEFAULT_PASSWORD S3_SECRET_ACCESS_KEY; do
  [[ -n "$(envget "$key")" ]] || die "$key is empty in $ENV_FILE."
done

# The production guard in app/core/config.py refuses to boot on these, but it
# does so inside a container whose logs nobody is reading yet.
[[ "$(envget APP_ENV)" == "production" ]] || warn "APP_ENV is not 'production' in $ENV_FILE."
[[ "$(envget STORAGE_PROVIDER)" != "local" ]] ||
  die "STORAGE_PROVIDER=local is refused in production by the application itself."
[[ "$(envget STORAGE_CONTAINER)" != "contracts" ]] ||
  die "STORAGE_CONTAINER=contracts shadows the SPA route /contracts/<uuid>; every
document download would return the application's HTML. Use cip-documents."

# MinIO reads its credentials from a different file to the application, so the two
# can disagree - and the symptom is not an authentication error at start but every
# upload failing with SignatureDoesNotMatch much later.
if [[ -f "$MINIO_ENV_FILE" ]]; then
  mu="$(sed -n 's/^MINIO_ROOT_USER=//p' "$MINIO_ENV_FILE" | tail -n 1)"
  mp="$(sed -n 's/^MINIO_ROOT_PASSWORD=//p' "$MINIO_ENV_FILE" | tail -n 1)"
  [[ "$mu" == "$(envget S3_ACCESS_KEY_ID)" ]] ||
    die "MINIO_ROOT_USER in $MINIO_ENV_FILE does not match S3_ACCESS_KEY_ID in $ENV_FILE."
  [[ "$mp" == "$(envget S3_SECRET_ACCESS_KEY)" ]] ||
    die "MINIO_ROOT_PASSWORD in $MINIO_ENV_FILE does not match S3_SECRET_ACCESS_KEY in $ENV_FILE."
fi
ok "Configuration looks consistent."

# Postgres is on the host, not in a container, and is the one dependency nothing
# here can start. Checked before anything else so a stopped database is reported
# as such rather than as a migration that failed for unclear reasons.
pg_host="$(envget POSTGRES_HOST)"; pg_port="$(envget POSTGRES_PORT)"
if [[ -n "$pg_host" ]] && command -v pg_isready >/dev/null 2>&1; then
  if pg_isready -h "$pg_host" -p "${pg_port:-5432}" -q; then
    ok "PostgreSQL is accepting connections at $pg_host:${pg_port:-5432}."
  else
    die "PostgreSQL is not accepting connections at $pg_host:${pg_port:-5432}.
  sudo systemctl status postgresql
  ss -lntp | grep 5432"
  fi
fi

# ---------------------------------------------------------------------------
# Start
# ---------------------------------------------------------------------------
echo
log "Starting units in dependency order"
for unit in "${CLEAR_UNITS[@]}"; do
  printf '  %-34s' "$unit"
  if sc start "$unit" >/dev/null 2>&1; then
    printf '%sstarted%s\n' "$C_GREEN" "$C_OFF"
  else
    printf '%sFAILED%s\n' "$C_RED" "$C_OFF"
    echo
    warn "$unit did not start. Its own log first:"
    sc status "$unit" --no-pager -l || true
    journalctl --user -u "$unit" -n 40 --no-pager || true
    die "Aborting; the services after $unit were not started."
  fi
done

echo
log "Waiting for health checks"
exec "$SCRIPT_DIR/health-check.sh" --wait
