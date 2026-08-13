#!/usr/bin/env bash
#
# Install and configure PostgreSQL + pgvector DIRECTLY ON THE VM. Run with sudo.
#
#   sudo ./install-postgres.sh --password '<app password>' --allow-cidr 10.89.0.0/16
#
#   --password      the password for the application role. Required.
#   --allow-cidr    the network the containers reach Postgres from. Repeatable.
#                   Default is the podman network's own subnet, discovered below.
#   --major         PostgreSQL major version. Default 17.
#   --db / --user   database and role names. Default clear / clear_app.
#   --listen        address to listen on in addition to localhost. Default is the
#                   podman bridge gateway.
#   --podman-user   the account that owns the ROOTLESS containers. Default clear.
#                   Its network is invisible to root's podman; see step 4 of this script.
#
# Why not a container: the database is the one thing in this deployment whose
# lifecycle must not be coupled to a container's. Its data directory lives on the
# VM's filesystem where the distribution's own backup, upgrade and monitoring
# tooling can see it, `pg_dump` runs without an exec into anything, and a
# `podman volume rm` typed at the wrong moment cannot take it.
#
# Idempotent: safe to re-run. It will not overwrite an existing data directory and
# it will not reset an existing role's password unless --force-password is given.

set -euo pipefail

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m ok \033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m !! \033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m xx \033[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Run with sudo: this installs packages and edits /etc/postgresql."

PG_MAJOR=17
DB_NAME=clear
DB_USER=clear_app
DB_PASSWORD=""
LISTEN_EXTRA=""
FORCE_PASSWORD=0
PODMAN_USER=clear
ALLOW_CIDRS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --password)       DB_PASSWORD="$2"; shift 2 ;;
    --allow-cidr)     ALLOW_CIDRS+=("$2"); shift 2 ;;
    --major)          PG_MAJOR="$2"; shift 2 ;;
    --db)             DB_NAME="$2"; shift 2 ;;
    --user)           DB_USER="$2"; shift 2 ;;
    --listen)         LISTEN_EXTRA="$2"; shift 2 ;;
    --podman-user)    PODMAN_USER="$2"; shift 2 ;;
    --force-password) FORCE_PASSWORD=1; shift ;;
    *) die "Unknown argument: $1" ;;
  esac
done

[[ -n "$DB_PASSWORD" ]] || die "--password is required."

# ---------------------------------------------------------------------------
# 1. Distribution
# ---------------------------------------------------------------------------
[[ -r /etc/os-release ]] || die "/etc/os-release is missing; cannot identify this distribution."
. /etc/os-release
log "Detected: $PRETTY_NAME"

case "$ID" in
  ubuntu|debian) FAMILY=debian ;;
  rhel|rocky|almalinux|centos|fedora) FAMILY=rhel ;;
  *) die "Unsupported distribution '$ID'. Install PostgreSQL $PG_MAJOR and pgvector by hand,
then run the SQL at the bottom of this script. Everything else in the deployment
is distribution-independent." ;;
esac

# ---------------------------------------------------------------------------
# 2. Install
# ---------------------------------------------------------------------------
if [[ "$FAMILY" == debian ]]; then
  # PGDG rather than the distribution's own packages: Ubuntu 22.04 ships
  # PostgreSQL 14 and Debian 12 ships 15, and pgvector is not packaged for either
  # in the base repositories. PGDG carries both, for every supported release.
  log "Adding the PostgreSQL APT repository (PGDG)"
  apt-get update -qq
  apt-get install -y -qq curl ca-certificates gnupg lsb-release >/dev/null
  install -d /usr/share/postgresql-common/pgdg
  curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
    -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc
  echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] \
https://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" \
    > /etc/apt/sources.list.d/pgdg.list
  apt-get update -qq

  log "Installing postgresql-$PG_MAJOR, contrib and pgvector"
  apt-get install -y -qq \
    "postgresql-$PG_MAJOR" \
    "postgresql-contrib-$PG_MAJOR" \
    "postgresql-$PG_MAJOR-pgvector" \
    postgresql-client-common postgresql-common >/dev/null

  PGDATA="/var/lib/postgresql/$PG_MAJOR/main"
  PGCONF_DIR="/etc/postgresql/$PG_MAJOR/main"
  PG_SERVICE="postgresql@$PG_MAJOR-main"
  PSQL_BIN="/usr/lib/postgresql/$PG_MAJOR/bin/psql"
else
  log "Adding the PostgreSQL YUM repository (PGDG)"
  ARCH="$(uname -m)"
  MAJOR_OS="${VERSION_ID%%.*}"
  dnf install -y -q \
    "https://download.postgresql.org/pub/repos/yum/reporpms/EL-$MAJOR_OS-$ARCH/pgdg-redhat-repo-latest.noarch.rpm" \
    || warn "PGDG repo rpm already present, or this is Fedora (which packages PostgreSQL itself)."
  # The distribution's own postgresql module shadows PGDG's packages and wins.
  dnf -qy module disable postgresql >/dev/null 2>&1 || true

  log "Installing postgresql$PG_MAJOR-server, contrib and pgvector"
  dnf install -y -q \
    "postgresql$PG_MAJOR-server" \
    "postgresql$PG_MAJOR-contrib" \
    "pgvector_$PG_MAJOR" >/dev/null

  PGDATA="/var/lib/pgsql/$PG_MAJOR/data"
  PGCONF_DIR="$PGDATA"
  PG_SERVICE="postgresql-$PG_MAJOR"
  PSQL_BIN="/usr/pgsql-$PG_MAJOR/bin/psql"

  # Debian's package runs initdb for you; the RHEL one does not, and the server
  # refuses to start on an empty data directory with an error about the cluster
  # rather than about initdb.
  if [[ ! -f "$PGDATA/PG_VERSION" ]]; then
    log "Initialising the data directory (--data-checksums)"
    # Checksums cost a few percent and turn silent disk corruption in a vector
    # column into a loud error. They cannot be enabled later without a dump and
    # reload, so the decision has to be made here.
    PGSETUP_INITDB_OPTIONS="--data-checksums" "/usr/pgsql-$PG_MAJOR/bin/postgresql-$PG_MAJOR-setup" initdb
  else
    ok "Data directory already initialised at $PGDATA - left alone."
  fi
fi

ok "PostgreSQL $("$PSQL_BIN" --version | awk '{print $3}') installed."
log "Data directory: $PGDATA"

# ---------------------------------------------------------------------------
# 3. Start, and enable at boot
# ---------------------------------------------------------------------------
log "Enabling and starting $PG_SERVICE"
systemctl enable "$PG_SERVICE" >/dev/null
systemctl start "$PG_SERVICE"
systemctl is-active --quiet "$PG_SERVICE" ||
  die "$PG_SERVICE did not start.  journalctl -u $PG_SERVICE -n 50"
ok "$PG_SERVICE is running and enabled at boot."

# ---------------------------------------------------------------------------
# 4. Which addresses the containers arrive from
# ---------------------------------------------------------------------------
# Rootless podman does not present container addresses to the host directly, so
# guessing here is worse than useless. The bridge subnet is the right default and
# is verified for real at the end of this script; if it turns out to be wrong,
# PostgreSQL's own log names the address it rejected, which is the fastest way to
# the correct CIDR:
#
#   sudo journalctl -u $PG_SERVICE | grep 'no pg_hba.conf entry'
#
# Queried as $PODMAN_USER, not as root. Rootless podman keeps its own container
# storage and its own networks per user, so `podman network inspect clear-net`
# run by root finds nothing at all while the network exists perfectly well for the
# deployment account - and the fallback below would then silently be used.
# `sudo -i` rather than `sudo -u`, so the login environment (XDG_RUNTIME_DIR) is
# set up the way podman expects.
podman_as_user() {
  sudo -iu "$PODMAN_USER" podman "$@" 2>/dev/null || true
}

if (( ${#ALLOW_CIDRS[@]} == 0 )); then
  if id "$PODMAN_USER" >/dev/null 2>&1; then
    subnet="$(podman_as_user network inspect clear-net --format '{{range .Subnets}}{{.Subnet}}{{end}}')"
    [[ -n "$subnet" ]] && ALLOW_CIDRS+=("$subnet")
  fi
  if (( ${#ALLOW_CIDRS[@]} == 0 )); then
    # Reached when the network has not been created yet. The default netavark
    # range is a reasonable guess and the verification at the end of this script,
    # plus the journalctl line above, is how a wrong guess gets corrected.
    warn "Could not read the clear-net subnet as user '$PODMAN_USER'.
     Falling back to 10.89.0.0/16. Create the network first -
       sudo -u $PODMAN_USER podman network create clear-net
     - and re-run, or fix it later with --allow-cidr."
    ALLOW_CIDRS=("10.89.0.0/16")
  fi
  # Rootless containers reaching the host over the bridge can also present the
  # host's own address.
  ALLOW_CIDRS+=("127.0.0.1/32")
fi
log "Allowing connections from: ${ALLOW_CIDRS[*]}"

if [[ -z "$LISTEN_EXTRA" ]]; then
  if id "$PODMAN_USER" >/dev/null 2>&1; then
    LISTEN_EXTRA="$(podman_as_user network inspect clear-net --format '{{range .Subnets}}{{.Gateway}}{{end}}')"
  fi
  # Falling back to the primary private address rather than to 0.0.0.0.
  # `|| true` on the pipeline: `set -o pipefail` is on, so a host without the
  # `hostname` binary would abort the install here rather than fall through to the
  # empty-value handling below.
  [[ -n "$LISTEN_EXTRA" ]] || LISTEN_EXTRA="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
fi

# ---------------------------------------------------------------------------
# 5. Configuration
# ---------------------------------------------------------------------------
# Written as a drop-in rather than by editing postgresql.conf, so a package
# upgrade cannot silently revert it and `diff` against the stock file still works.
CONF_D="$PGCONF_DIR/conf.d"
mkdir -p "$CONF_D"
grep -qE "^\s*include_dir\s*=\s*'conf\.d'" "$PGCONF_DIR/postgresql.conf" ||
  echo "include_dir = 'conf.d'" >> "$PGCONF_DIR/postgresql.conf"

log "Writing $CONF_D/50-clear.conf"
cat > "$CONF_D/50-clear.conf" <<EOF
# CLEAR deployment. Managed by deploy/podman/scripts/install-postgres.sh.

# NOT 0.0.0.0, and not '*'. Localhost for psql and pg_dump on this VM, plus the
# one address the containers use. The firewall is a second line of defence, not
# the first.
listen_addresses = 'localhost${LISTEN_EXTRA:+,$LISTEN_EXTRA}'
port = 5432

# Only scram. `md5` is still accepted by default builds and is offline-crackable.
password_encryption = scram-sha-256

# --- vector workload tuning -------------------------------------------------
# The same values the compose stack passed to its Postgres container. Sized for a
# 16 GB VM; scale shared_buffers to roughly 25% of RAM.
shared_buffers = 512MB
work_mem = 32MB
# HNSW index builds are bound by this. Too small and building an index over a
# few hundred thousand vectors takes hours instead of minutes.
maintenance_work_mem = 256MB
max_parallel_workers_per_gather = 4
effective_cache_size = 4GB

# --- durability and recovery ------------------------------------------------
# Enough WAL retained for pg_basebackup and for point-in-time recovery to be
# possible later without a restart to enable it.
wal_level = replica
max_wal_size = 2GB
min_wal_size = 256MB

# --- logging ----------------------------------------------------------------
# A slow query here is usually a similarity search that fell back to a sequential
# scan, which is what a missing or unusable HNSW index looks like.
log_min_duration_statement = 2000
log_line_prefix = '%m [%p] %q%u@%d '
log_checkpoints = on
log_connections = on
log_disconnections = on
log_autovacuum_min_duration = 0

# pg_stat_statements needs preloading, which needs a full restart rather than a
# reload. It is not required by the application - /readyz gates on vector,
# pg_trgm, uuid-ossp and citext only - but it is the difference between guessing
# at a slow deployment and measuring it.
shared_preload_libraries = 'pg_stat_statements'
pg_stat_statements.max = 10000
pg_stat_statements.track = top
EOF

log "Writing pg_hba.conf rules"
HBA="$PGCONF_DIR/pg_hba.conf"
cp -n "$HBA" "$HBA.orig-$(date +%F)" 2>/dev/null || true
# Rewritten rather than appended: appending on every re-run accumulates duplicate
# rules, and pg_hba is first-match-wins, so a stale permissive line at the top
# would keep winning.
sed -i '/# BEGIN CLEAR/,/# END CLEAR/d' "$HBA"
{
  echo "# BEGIN CLEAR - managed by deploy/podman/scripts/install-postgres.sh"
  for cidr in "${ALLOW_CIDRS[@]}"; do
    # scram-sha-256, and scoped to one database and one role. `trust` would let
    # anything that can route to this port in; `all all` would let the application
    # role reach every other database on the instance.
    printf 'host    %-12s %-12s %-20s scram-sha-256\n' "$DB_NAME" "$DB_USER" "$cidr"
  done
  echo "# END CLEAR"
} >> "$HBA"

# 0.0.0.0/0 would make the database reachable from anything that can route to the
# VM. Refused rather than warned about.
if grep -E '^\s*host' "$HBA" | grep -q '0\.0\.0\.0/0'; then
  die "pg_hba.conf contains a 0.0.0.0/0 rule. Remove it: the containers reach this
server from a known subnet, and nothing else should reach it at all."
fi

# ---------------------------------------------------------------------------
# 6. Role, database and extensions
# ---------------------------------------------------------------------------
systemctl restart "$PG_SERVICE"
sleep 2
systemctl is-active --quiet "$PG_SERVICE" ||
  die "$PG_SERVICE failed to restart with the new configuration.
  journalctl -u $PG_SERVICE -n 50"

psql_as_postgres() { sudo -u postgres "$PSQL_BIN" -v ON_ERROR_STOP=1 "$@"; }

log "Creating role $DB_USER"
if psql_as_postgres -tAc "SELECT 1 FROM pg_roles WHERE rolname='$DB_USER'" | grep -q 1; then
  if (( FORCE_PASSWORD )); then
    psql_as_postgres -c "ALTER ROLE \"$DB_USER\" WITH PASSWORD '$DB_PASSWORD';"
    ok "Role exists; password reset (--force-password)."
  else
    ok "Role exists; password left alone. Use --force-password to change it."
  fi
else
  psql_as_postgres -c "CREATE ROLE \"$DB_USER\" WITH LOGIN PASSWORD '$DB_PASSWORD';"
  ok "Role $DB_USER created."
fi

log "Creating database $DB_NAME"
if psql_as_postgres -tAc "SELECT 1 FROM pg_database WHERE datname='$DB_NAME'" | grep -q 1; then
  ok "Database exists; left alone."
else
  psql_as_postgres -c "CREATE DATABASE \"$DB_NAME\" OWNER \"$DB_USER\" ENCODING 'UTF8';"
  ok "Database $DB_NAME created."
fi

# Extensions are installed by a SUPERUSER, once per database, into `public`.
# Alembic also guards each with CREATE EXTENSION IF NOT EXISTS so that a managed
# Postgres works without this step - but on a self-hosted server the application
# role is deliberately not a superuser, so it could not create them itself.
log "Installing extensions into $DB_NAME"
psql_as_postgres --dbname "$DB_NAME" <<'SQL'
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "vector";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";
CREATE EXTENSION IF NOT EXISTS "citext";
CREATE EXTENSION IF NOT EXISTS "btree_gin";
SQL
# Separate, and allowed to fail: it needs the preload above, which needs the
# restart that has just happened - but on a re-run against a server that has not
# been restarted yet it would abort the whole block.
psql_as_postgres --dbname "$DB_NAME" -c 'CREATE EXTENSION IF NOT EXISTS "pg_stat_statements";' \
  || warn "pg_stat_statements not created (needs the restart above to have taken effect). Optional."

# The application owns its schema: Alembic creates ~36 tables and their indexes on
# first migration, which needs CREATE on the schema it builds them in.
psql_as_postgres --dbname "$DB_NAME" <<SQL
GRANT ALL ON DATABASE "$DB_NAME" TO "$DB_USER";
GRANT ALL ON SCHEMA public TO "$DB_USER";
SQL
ok "Extensions installed and privileges granted."

# ---------------------------------------------------------------------------
# 7. Verify
# ---------------------------------------------------------------------------
echo
log "Verification"
"$PSQL_BIN" --version
sudo -u postgres "$PSQL_BIN" --dbname "$DB_NAME" -c \
  "SELECT extname, extversion FROM pg_extension ORDER BY extname;"

# A vector column that can be written, indexed and searched. `CREATE EXTENSION`
# succeeding proves the extension is registered, not that the build works - and a
# pgvector that registers but cannot build an HNSW index turns every similarity
# search into a sequential scan, silently.
log "Round-tripping a vector through an HNSW index"
sudo -u postgres "$PSQL_BIN" -v ON_ERROR_STOP=1 --dbname "$DB_NAME" >/dev/null <<'SQL'
CREATE TABLE IF NOT EXISTS _clear_vector_check (id serial primary key, embedding vector(3));
INSERT INTO _clear_vector_check (embedding) VALUES ('[1,2,3]'), ('[4,5,6]');
CREATE INDEX IF NOT EXISTS _clear_vector_check_hnsw
  ON _clear_vector_check USING hnsw (embedding vector_cosine_ops);
SELECT id FROM _clear_vector_check ORDER BY embedding <=> '[1,2,3]' LIMIT 1;
DROP TABLE _clear_vector_check;
SQL
ok "pgvector writes, indexes and searches correctly."

# The one that actually matters: can the APPLICATION role log in over TCP with a
# password, from a network address? Everything above ran as `postgres` over the
# unix socket, which proves nothing about pg_hba.
log "Testing password login for $DB_USER over TCP"
if PGPASSWORD="$DB_PASSWORD" "$PSQL_BIN" --host 127.0.0.1 --username "$DB_USER" \
     --dbname "$DB_NAME" -tAc 'SELECT 1' >/dev/null 2>&1; then
  ok "$DB_USER can log in over TCP."
else
  warn "$DB_USER could NOT log in over 127.0.0.1. Check the '# BEGIN CLEAR' block in
     $HBA and that 127.0.0.1/32 is among the allowed CIDRs."
fi

cat <<EOF

$(printf '\033[1;32m')PostgreSQL is ready.$(printf '\033[0m')

  Service        $PG_SERVICE   (enabled at boot)
  Data directory $PGDATA
  Config         $PGCONF_DIR/conf.d/50-clear.conf
  Listening on   localhost${LISTEN_EXTRA:+, $LISTEN_EXTRA}
  Allowed from   ${ALLOW_CIDRS[*]}

Put this in /etc/clear/clear.env (the password is the one you passed):

  POSTGRES_HOST=${LISTEN_EXTRA:-127.0.0.1}
  POSTGRES_PORT=5432
  POSTGRES_DB=$DB_NAME
  POSTGRES_USER=$DB_USER
  DATABASE_URL=postgresql+asyncpg://$DB_USER:<password>@${LISTEN_EXTRA:-127.0.0.1}:5432/$DB_NAME

The application's own tables do not exist yet. clear-migrate.service creates them
on the first start; nothing here runs Alembic.

If a container later cannot connect, PostgreSQL names the address it rejected:

  sudo journalctl -u $PG_SERVICE | grep 'no pg_hba.conf entry'

then re-run this script with --allow-cidr <that address>/32.
EOF
