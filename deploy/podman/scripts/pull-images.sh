#!/usr/bin/env bash
#
# Pull every image this deployment runs. Production VM.
#
#   ./pull-images.sh              # the tag in images.env
#   ./pull-images.sh v1.0.1       # a specific tag, without changing images.env
#
# Pulling is separated from starting on purpose. A pull is the slow, network-bound,
# failure-prone half of an update; doing it while the current version is still
# serving means the actual switchover is a container restart measured in seconds,
# and a registry outage costs nothing because nothing has been stopped yet.

. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

load_images_env
[[ -n "${1:-}" ]] && IMAGE_TAG="$1"
require_image_coordinates
require_podman

log "Application images  $REGISTRY/$REGISTRY_PROJECT/*:$IMAGE_TAG"
for spec in "${APP_IMAGES[@]}"; do
  IFS=: read -r name _ _ _ <<<"$spec"
  ref="$(image_ref "$name")"
  log "Pulling $ref"
  podman pull "$ref" || die "Could not pull $ref.
  - Authenticated?      $INSTALL_DIR/scripts/login-registry.sh
  - Tag really pushed?  podman manifest inspect $ref
  - Egress to $REGISTRY from this VM?"
  ok "$ref"
done

echo
log "Third-party images"
for ref in "$REDIS_IMAGE" "$MINIO_IMAGE" "$MC_IMAGE"; do
  log "Pulling $ref"
  podman pull "$ref" || die "Could not pull $ref."
  ok "$ref"
done

# A registry that is unreachable at 03:00 must not be able to stop a rebooted VM
# from coming back. Everything above is now in local storage, so it will not be.
echo
log "Local image store"
podman images --format "table {{.Repository}}:{{.Tag}}\t{{.Size}}\t{{.Created}}" |
  grep -E "clear-|redis|minio|REPOSITORY" || true

cat <<EOF

Nothing is running differently yet - a pull only fills the local store.

  $INSTALL_DIR/scripts/update.sh $IMAGE_TAG     # switch to it, with a health gate
  $INSTALL_DIR/scripts/start.sh                 # first-ever start
EOF
