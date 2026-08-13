#!/usr/bin/env bash
#
# Return to a previously deployed version.
#
#   ./rollback.sh              # to PREVIOUS_IMAGE_TAG, recorded by update.sh
#   ./rollback.sh v1.0.0       # to a specific tag
#
# What this DOES roll back:
#   * the container images, on every application service
#   * the unit files, which name them
#   * the recorded IMAGE_TAG
#
# What it does NOT roll back, and cannot:
#   * the DATABASE SCHEMA. If the release being backed out ran a migration, the
#     old image is now talking to a newer schema. Additive migrations - a new
#     table, a new nullable column - are harmless that way round, which is most
#     of them. A migration that DROPPED or RETYPED something is not, and the old
#     image will fail on the missing column.
#         podman run --rm --env-file <env> <image>:<new-tag> python -m app.cli downgrade -1
#     Run it with the NEW image, before this script: the downgrade script only
#     exists in the release that introduced it.
#   * OBJECT STORAGE and uploaded documents. Nothing here touches them.
#   * clear.env. Configuration is not versioned with the image; if the release
#     needed a new setting, remove it by hand.
#
# When a database restore is required instead: any release that destroyed data
# (a dropped column, a data-rewriting migration you cannot reverse). Then the
# order is stop -> restore the dump -> restore the object store from the same
# night -> roll the image back. See PODMAN_DEPLOYMENT.md §12.

. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

load_images_env
require_podman
require_user_systemd

TARGET="${1:-$PREVIOUS_IMAGE_TAG}"
[[ -n "$TARGET" ]] || die "No PREVIOUS_IMAGE_TAG recorded in $IMAGES_ENV and no tag given.
Pass one explicitly:  ./rollback.sh v1.0.0
Tags still in the local image store:
$(podman images --format '  {{.Repository}}:{{.Tag}}' | grep clear-backend || echo '  (none)')"

[[ "$TARGET" != "$IMAGE_TAG" ]] || die "$TARGET is already the deployed tag."

log "Rolling back:  $IMAGE_TAG  ->  $TARGET"

# Pull only if it is not already local. A rollback that depends on the registry
# being up is a rollback that fails during the outage it exists for.
missing=0
for spec in "${APP_IMAGES[@]}"; do
  IFS=: read -r name _ _ _ <<<"$spec"
  podman image exists "$(image_ref "$name" "$TARGET")" || missing=1
done
if (( missing )); then
  warn "$TARGET is not fully present locally; pulling it."
  "$SCRIPT_DIR/pull-images.sh" "$TARGET" >/dev/null ||
    die "Could not pull $TARGET, and it is not in the local store. Nothing changed."
else
  ok "$TARGET is already in the local image store."
fi

log "Re-rendering unit files at $TARGET"
IMAGE_TAG="$TARGET" "$SCRIPT_DIR/install-quadlet.sh" >/dev/null

log "Restarting application containers"
for unit in clear-backend clear-worker-parser clear-worker-ai clear-queue clear-frontend; do
  printf '  %-28s' "$unit"
  if sc restart "$unit.service" >/dev/null 2>&1; then
    printf '%srestarted%s\n' "$C_GREEN" "$C_OFF"
  else
    printf '%sFAILED%s\n' "$C_RED" "$C_OFF"
    journalctl --user -u "$unit.service" -n 40 --no-pager || true
  fi
done

# The tag we are leaving becomes the way forward again, so a rollback can itself
# be undone without looking anything up.
tmp="$(mktemp)"
grep -vE '^(IMAGE_TAG|PREVIOUS_IMAGE_TAG)=' "$IMAGES_ENV" > "$tmp" || true
printf 'IMAGE_TAG=%s\nPREVIOUS_IMAGE_TAG=%s\n' "$TARGET" "$IMAGE_TAG" >> "$tmp"
cat "$tmp" > "$IMAGES_ENV"
rm -f "$tmp"

echo
log "Verifying"
"$SCRIPT_DIR/health-check.sh" --wait || die "Rolled back to $TARGET, but it is not healthy either.
This is now an incident rather than a bad release: check the database and Redis
before trying another image."

echo
ok "Rolled back to $TARGET."
warn "The database schema was NOT rolled back. If the backed-out release migrated it,
re-read the header of this script."
