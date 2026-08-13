#!/usr/bin/env bash
#
# Back up everything that cannot be rebuilt from the registry and the repository.
#
#   ./backup.sh                       # to /var/backups/clear
#   ./backup.sh /mnt/backups          # somewhere else
#
# Three artefacts, taken in this order and restored in this order:
#
#   1. PostgreSQL       pg_dump -Fc. The host's database, not a container's.
#   2. Document storage the `clear-storage` volume: the bytes of every uploaded
#                       contract and every generated export.
#   3. Configuration    clear.env, images.env, the rendered units and the host
#                       nginx config.
#
# The first two MUST be treated as one backup. `documents.storage_path` in
# Postgres is a path into the storage volume, so a database restored newer than
# the volume references files that do not exist - and the application looks
# perfectly healthy while every document download 404s. Restoring the volume newer
# than the database leaves orphans nothing can reach, which is merely wasteful.
# Never restore one without the other.
#
# There is no object store in this deployment: STORAGE_PROVIDER=local, so the
# volume in step 2 IS the document store rather than a cache of one.
#
# NOT backed up, deliberately:
#   * the Redis volume. It holds in-flight queue state worth minutes; restoring a
#     day-old copy would replay work already done.
#   * images. They are in the registry, which is what a registry is for.
#
# Schedule it from the deployment user's crontab:
#   0 2 * * *  /opt/clear/deploy/podman/scripts/backup.sh >> /var/log/clear-backup.log 2>&1

. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

load_images_env
require_podman

DEST="${1:-/var/backups/clear}"

RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-14}"
STAMP="$(date +%Y-%m-%d-%H%M)"
OUT="$DEST/$STAMP"

mkdir -p "$OUT" || die "Cannot create $OUT. Create $DEST and give $(id -un) write access."
# The dump contains every contract in the system in plain text.
chmod 700 "$DEST" "$OUT"

[[ -f "$ENV_FILE" ]] || die "$ENV_FILE does not exist; there is nothing to read the database
credentials from. Set ENV_FILE, or run this on the deployment host."

envget() { sed -n "s/^$1=//p" "$ENV_FILE" | tail -n 1; }
PGHOST="$(envget POSTGRES_HOST)"; PGPORT="$(envget POSTGRES_PORT)"
PGUSER="$(envget POSTGRES_USER)"; PGDATABASE="$(envget POSTGRES_DB)"
PGPASSWORD="$(envget POSTGRES_PASSWORD)"; export PGPASSWORD

log "Backing up to $OUT"

# --- 1. PostgreSQL ----------------------------------------------------------
log "1/3  PostgreSQL  $PGUSER@$PGHOST:${PGPORT:-5432}/$PGDATABASE"
command -v pg_dump >/dev/null 2>&1 ||
  die "pg_dump is not installed on this host. It is in postgresql-client."

# -Fc, the custom format: compressed, and restorable table-by-table with
# pg_restore. A plain SQL dump of a database with millions of vector rows is
# enormous and can only be restored whole.
pg_dump --host "$PGHOST" --port "${PGPORT:-5432}" --username "$PGUSER" \
        --dbname "$PGDATABASE" --format=custom --compress=6 \
        --file "$OUT/postgres-$PGDATABASE.dump" ||
  die "pg_dump failed. Nothing else has been written."
ok "$(du -h "$OUT/postgres-$PGDATABASE.dump" | cut -f1)  postgres-$PGDATABASE.dump"

# --- 2. Document storage ----------------------------------------------------
log "2/3  Document storage (volume $STORAGE_VOLUME)"
if podman volume exists "$STORAGE_VOLUME" 2>/dev/null; then
  # `podman volume export` streams the volume as a tar without needing a helper
  # container or knowing where podman keeps its overlay directories.
  podman volume export "$STORAGE_VOLUME" --output "$OUT/$STORAGE_VOLUME.tar" ||
    die "Could not export $STORAGE_VOLUME."
  gzip -f "$OUT/$STORAGE_VOLUME.tar"
  ok "$(du -h "$OUT/$STORAGE_VOLUME.tar.gz" | cut -f1)  $STORAGE_VOLUME.tar.gz"
else
  # Not a warning. This volume holds every contract in the system, so its absence
  # means either the deployment is not installed or the backup is being taken
  # against the wrong host - and a "successful" backup with no documents in it is
  # the worst possible outcome, because it is only discovered during a restore.
  die "Volume $STORAGE_VOLUME does not exist. This is where every uploaded document
lives, so a backup without it is not a backup. Check that the deployment is
installed on this host and that you are the deployment user:
  podman volume ls"
fi

# --- 3. Configuration -------------------------------------------------------
log "3/3  Configuration"
mkdir -p "$OUT/config"
for f in "$ENV_FILE" "$IMAGES_ENV"; do
  [[ -f "$f" ]] && cp -p "$f" "$OUT/config/" && ok "$(basename "$f")"
done
if compgen -G "$HOME/.config/containers/systemd/clear-*" >/dev/null; then
  mkdir -p "$OUT/config/quadlet"
  cp -p "$HOME"/.config/containers/systemd/clear-* "$OUT/config/quadlet/" 2>/dev/null || true
  cp -p "$HOME"/.config/containers/systemd/clear.network "$OUT/config/quadlet/" 2>/dev/null || true
  ok "rendered quadlet units"
fi
if compgen -G "$HOME/.config/systemd/user/clear-*.service" >/dev/null; then
  mkdir -p "$OUT/config/systemd"
  cp -p "$HOME"/.config/systemd/user/clear-*.service "$OUT/config/systemd/" || true
  ok "rendered systemd units"
fi
for f in /etc/nginx/sites-available/clear.conf /etc/nginx/conf.d/clear.conf; do
  [[ -f "$f" ]] && cp -p "$f" "$OUT/config/" && ok "$(basename "$f") (host nginx)"
done

# The config copies hold every secret this deployment has.
chmod -R go-rwx "$OUT/config"

# --- verify -----------------------------------------------------------------
echo
log "Verifying"
# A backup nobody has read back is a hypothesis. pg_restore --list parses the
# whole archive and fails on a truncated one, which a size check would not catch.
pg_restore --list "$OUT/postgres-$PGDATABASE.dump" >/dev/null ||
  die "The dump does not parse. Treat this backup as failed."
# `grep -c` exits 1 when the count is zero, and pipefail would turn that into a
# failed backup script one line after a backup that actually succeeded.
tables="$(pg_restore --list "$OUT/postgres-$PGDATABASE.dump" | grep -c 'TABLE DATA' || true)"
ok "Dump parses; ${tables:-0} tables with data."
[[ "${tables:-0}" -gt 0 ]] || warn "The dump contains no table data. On a live deployment that is a red flag,
     not a clean backup - check that POSTGRES_DB names the right database."

# --- retention --------------------------------------------------------------
log "Pruning backups older than $RETENTION_DAYS days"
find "$DEST" -maxdepth 1 -type d -name '20*' -mtime "+$RETENTION_DAYS" \
  -exec rm -rf {} + 2>/dev/null || true

echo
ok "Backup complete: $OUT   ($(du -sh "$OUT" | cut -f1))"
cat <<EOF

$(printf '%s' "$C_YELLOW")A copy that only exists on this VM is not a backup.$(printf '%s' "$C_OFF") Ship it off the host:

  rsync -a --delete $OUT <offsite>:/backups/clear/

Restore (both artefacts, together - see the header of this script):

  $INSTALL_DIR/scripts/stop.sh
  pg_restore --host $PGHOST --username $PGUSER --dbname $PGDATABASE --clean --if-exists \\
             $OUT/postgres-$PGDATABASE.dump
  gunzip -c $OUT/$STORAGE_VOLUME.tar.gz > /tmp/$STORAGE_VOLUME.tar
  # Destructive: this discards whatever documents are on the volume now.
  podman volume rm $STORAGE_VOLUME && podman volume create $STORAGE_VOLUME
  podman volume import $STORAGE_VOLUME /tmp/$STORAGE_VOLUME.tar
  $INSTALL_DIR/scripts/start.sh
EOF
