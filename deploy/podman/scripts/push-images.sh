#!/usr/bin/env bash
#
# Push the three application images to the registry. Build machine only.
#
#   ./push-images.sh v1.0.0
#   ./push-images.sh                # uses IMAGE_TAG from images.env / the env
#
# It verifies each push by reading the manifest back from the registry rather
# than trusting the exit code, because a push to a repository the account can
# write but not read is a failure that only shows up on the VM at pull time.

. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

load_images_env
IMAGE_TAG="${1:-${IMAGE_TAG:-}}"
require_image_coordinates
require_podman

if [[ "$IMAGE_TAG" == "latest" ]]; then
  die "Refusing to push 'latest' as the only tag. It is not a version: it cannot
be rolled back to, and a VM running it cannot say what it is running. Use
v1.0.0, 2026-08-12-001 or git-<sha>."
fi

log "Pushing $REGISTRY/$REGISTRY_PROJECT/*:$IMAGE_TAG"
echo

for spec in "${APP_IMAGES[@]}"; do
  IFS=: read -r name _ _ _ <<<"$spec"
  ref="$(image_ref "$name")"

  podman image exists "$ref" || die "$ref is not present locally. Run ./build-images.sh $IMAGE_TAG first."

  log "Pushing $ref"
  podman push "$ref"
done

echo
log "Verifying each tag by reading it back from the registry"
failed=0
for spec in "${APP_IMAGES[@]}"; do
  IFS=: read -r name _ _ _ <<<"$spec"
  ref="$(image_ref "$name")"
  if digest="$(podman manifest inspect "$ref" 2>/dev/null | sed -n 's/.*"digest": "\(sha256:[a-f0-9]*\)".*/\1/p' | head -n 1)"; then
    ok "$ref  ${digest:-present}"
  else
    warn "$ref could not be read back from the registry."
    failed=1
  fi
done
(( failed == 0 )) || die "At least one image is not readable from the registry. Do not deploy this tag."

cat <<EOF

On the production VM:

  sudo -u clear -i
  echo 'IMAGE_TAG=$IMAGE_TAG' >> $IMAGES_ENV     # or edit it in place
  $INSTALL_DIR/scripts/pull-images.sh
  $INSTALL_DIR/scripts/update.sh $IMAGE_TAG
EOF
