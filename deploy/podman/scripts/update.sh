#!/usr/bin/env bash
#
# Deploy a new version, with a health gate and an automatic way back.
#
#   ./update.sh v1.0.1
#
# The order below is the whole point of the script:
#
#   1. pull            slow, network-bound, and reversible - the old version is
#                      still serving throughout
#   2. record          the currently deployed tag becomes PREVIOUS_IMAGE_TAG
#   3. re-render       the unit files now name the new tag
#   4. migrate         Type=oneshot, so systemd waits for it to exit 0. A failed
#                      migration stops here, with nothing restarted
#   5. restart         the application containers, in dependency order
#   6. health gate     if it does not come up, roll back
#
# Migrations are the asymmetry. Steps 3-5 are reversible by putting the old tag
# back; step 4 usually is not. Read the "Database migration rollback" section of
# PODMAN_DEPLOYMENT.md §11 BEFORE deploying a release that drops or rewrites a
# column, because the automatic rollback below restores the previous IMAGE, not
# the previous SCHEMA.

. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

load_images_env
require_podman
require_user_systemd

NEW_TAG="${1:-}"
[[ -n "$NEW_TAG" ]] || die "Usage: ./update.sh <tag>      e.g. ./update.sh v1.0.1"
[[ "$NEW_TAG" != "latest" ]] || die "'latest' is not a version and cannot be rolled back to."

CURRENT_TAG="$IMAGE_TAG"
[[ -n "$CURRENT_TAG" ]] || die "IMAGE_TAG is not set in $IMAGES_ENV; there is nothing to update from."

if [[ "$NEW_TAG" == "$CURRENT_TAG" ]]; then
  warn "$NEW_TAG is already the deployed tag. Re-deploying it anyway."
fi

[[ -w "$IMAGES_ENV" ]] || die "$IMAGES_ENV is not writable by $(id -un); update.sh has to record the previous tag there."

log "Current  $CURRENT_TAG"
log "New      $NEW_TAG"
echo

# --- 1. pull, while the old version keeps serving ---------------------------
log "1/6  Pulling $NEW_TAG"
IMAGE_TAG="$NEW_TAG" "$SCRIPT_DIR/pull-images.sh" "$NEW_TAG" >/dev/null ||
  die "Pull failed. Nothing has been changed; the current version is still running."
ok "Images for $NEW_TAG are in the local store."

# --- 2. record the way back -------------------------------------------------
log "2/6  Recording $CURRENT_TAG as the rollback target"
# Rewritten rather than appended, so the file does not accumulate stale
# assignments where the last one silently wins.
tmp="$(mktemp)"
grep -vE '^(IMAGE_TAG|PREVIOUS_IMAGE_TAG)=' "$IMAGES_ENV" > "$tmp" || true
printf 'IMAGE_TAG=%s\nPREVIOUS_IMAGE_TAG=%s\n' "$NEW_TAG" "$CURRENT_TAG" >> "$tmp"
cat "$tmp" > "$IMAGES_ENV"
rm -f "$tmp"
ok "PREVIOUS_IMAGE_TAG=$CURRENT_TAG"

roll_back() {
  echo
  warn "Rolling back to $CURRENT_TAG"
  IMAGE_TAG="$CURRENT_TAG" "$SCRIPT_DIR/install-quadlet.sh" >/dev/null
  for unit in clear-backend clear-worker-parser clear-worker-ai clear-queue clear-frontend; do
    sc restart "$unit.service" >/dev/null 2>&1 || true
  done
  tmp="$(mktemp)"
  grep -vE '^(IMAGE_TAG|PREVIOUS_IMAGE_TAG)=' "$IMAGES_ENV" > "$tmp" || true
  printf 'IMAGE_TAG=%s\n' "$CURRENT_TAG" >> "$tmp"
  cat "$tmp" > "$IMAGES_ENV"
  rm -f "$tmp"
  die "Rolled back to $CURRENT_TAG. The SCHEMA was not rolled back - if the failed
release migrated the database, see PODMAN_DEPLOYMENT.md §11."
}

# --- 3. re-render the units at the new tag ----------------------------------
log "3/6  Re-rendering unit files at $NEW_TAG"
IMAGE_TAG="$NEW_TAG" "$SCRIPT_DIR/install-quadlet.sh" >/dev/null || roll_back
ok "Units name $NEW_TAG."

# --- 4. migrate -------------------------------------------------------------
log "4/6  Running migrations"
# RemainAfterExit=yes means the unit is 'active (exited)'; restart re-runs it.
# `alembic upgrade head` is a no-op when the schema is already current.
if ! sc restart clear-migrate.service; then
  journalctl --user -u clear-migrate.service -n 60 --no-pager || true
  roll_back
fi
ok "Schema is at head."

# --- 5. replace the application containers ----------------------------------
log "5/6  Restarting application containers"
for unit in clear-backend clear-worker-parser clear-worker-ai clear-queue clear-frontend; do
  printf '  %-28s' "$unit"
  if sc restart "$unit.service" >/dev/null 2>&1; then
    printf '%srestarted%s\n' "$C_GREEN" "$C_OFF"
  else
    printf '%sFAILED%s\n' "$C_RED" "$C_OFF"
    journalctl --user -u "$unit.service" -n 40 --no-pager || true
    roll_back
  fi
done

# --- 6. health gate ---------------------------------------------------------
echo
log "6/6  Health gate"
if ! "$SCRIPT_DIR/health-check.sh" --wait; then
  roll_back
fi

echo
ok "Deployed $NEW_TAG."
dim "Rollback target: $CURRENT_TAG   ->   $SCRIPT_DIR/rollback.sh"

# The previous images are kept deliberately. A rollback that has to pull from a
# registry is a rollback that fails when the registry is the thing that is down.
dim "Previous images are kept locally. Reclaim disk only once $NEW_TAG has proven itself:"
dim "  podman image prune --all --filter 'until=168h'"
