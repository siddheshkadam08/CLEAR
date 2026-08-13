#!/usr/bin/env bash
#
# Build the three application images. Runs on a BUILD MACHINE, not on the
# production VM.
#
#   REGISTRY=ghcr.io REGISTRY_PROJECT=acme/clear ./build-images.sh v1.0.0
#   ./build-images.sh                      # tag defaults to git-<short sha>
#
# Why not on the VM: the backend image installs LibreOffice, Tesseract, Poppler
# and a compiler toolchain, which is minutes of CPU and several gigabytes of
# intermediate layers on a host that is meant to be serving. A build there also
# means the running version is whatever the working tree happened to contain,
# which no registry can attest to and no rollback can return to.
#
# The frontend's VITE_* values are BAKED IN here. Vite substitutes
# import.meta.env at build time, so an image built with the wrong
# VITE_API_BASE_URL cannot be corrected by an environment variable on the VM - it
# has to be rebuilt. They are read from the env file so that one file describes
# the whole deployment.

. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

load_images_env
IMAGE_TAG="${1:-${IMAGE_TAG:-}}"

# A tag that identifies the commit, when none was given. `latest` is deliberately
# never produced: it is not a version, it cannot be rolled back to, and a VM that
# pulls it has no way to say what it is running.
if [[ -z "$IMAGE_TAG" ]]; then
  if git -C "$REPO_ROOT" rev-parse --short HEAD >/dev/null 2>&1; then
    IMAGE_TAG="git-$(git -C "$REPO_ROOT" rev-parse --short HEAD)"
  else
    die "No tag given and this is not a git checkout. Pass one: ./build-images.sh v1.0.0"
  fi
fi
require_image_coordinates
require_podman

# A build from a dirty tree produces an image whose tag names a commit it does
# not contain. Worth a warning, not a refusal - a hotfix build is a real thing.
if git -C "$REPO_ROOT" rev-parse HEAD >/dev/null 2>&1; then
  if [[ -n "$(git -C "$REPO_ROOT" status --porcelain)" ]]; then
    warn "Working tree has uncommitted changes; $IMAGE_TAG will not match the commit."
  fi
fi

# Frontend build arguments, read from the deployment's env template so that the
# bundle and the containers agree. Fall back to the committed example when the
# real file is elsewhere.
FRONTEND_ENV="${FRONTEND_ENV:-$ENV_FILE}"
[[ -f "$FRONTEND_ENV" ]] || FRONTEND_ENV="$DEPLOY_DIR/.env.production.example"

read_env() {
  # Deliberately not `source`: that file is written for podman --env-file, not
  # for a shell, and a stray character in a secret must not become a command.
  sed -n "s/^$1=//p" "$FRONTEND_ENV" | tail -n 1
}

VITE_API_BASE_URL="${VITE_API_BASE_URL:-$(read_env VITE_API_BASE_URL)}"
VITE_ENABLE_MICROSOFT_SSO="${VITE_ENABLE_MICROSOFT_SSO:-$(read_env VITE_ENABLE_MICROSOFT_SSO)}"
: "${VITE_API_BASE_URL:=/api/v1}"
: "${VITE_ENABLE_MICROSOFT_SSO:=false}"

# No storage build arguments. Document bytes are served by the API from local
# filesystem storage, so the frontend's nginx has one upstream - the backend - and
# it is set on the container at runtime rather than baked in here.

log "Registry ...... $REGISTRY/$REGISTRY_PROJECT"
log "Tag ........... $IMAGE_TAG"
log "Frontend args . VITE_API_BASE_URL=$VITE_API_BASE_URL  SSO=$VITE_ENABLE_MICROSOFT_SSO"
echo

cd "$REPO_ROOT"

for spec in "${APP_IMAGES[@]}"; do
  IFS=: read -r name context dockerfile target <<<"$spec"
  ref="$(image_ref "$name")"
  log "Building $name -> $ref"

  args=(build --tag "$ref" --file "$dockerfile" --target "$target")

  # --format=docker rather than podman's native OCI. Some registries - notably
  # older Docker Distribution and a few vendor-hosted ones - reject an OCI
  # manifest with an error about an unsupported media type that names neither
  # OCI nor the manifest.
  args+=(--format docker)

  # Stamped onto the image so a running container can be traced back to a commit
  # without trusting the tag.
  args+=(--label "org.opencontainers.image.version=$IMAGE_TAG")
  if git rev-parse HEAD >/dev/null 2>&1; then
    args+=(--label "org.opencontainers.image.revision=$(git rev-parse HEAD)")
  fi

  if [[ "$name" == "clear-frontend" ]]; then
    args+=(--build-arg "VITE_API_BASE_URL=$VITE_API_BASE_URL")
    args+=(--build-arg "VITE_ENABLE_MICROSOFT_SSO=$VITE_ENABLE_MICROSOFT_SSO")
    # Runtime wiring is also set on the container by the Quadlet unit; baking the
    # same values keeps `podman run` of this image alone workable for debugging.
    args+=(--build-arg "API_HOST=clear-backend")
    args+=(--build-arg "API_PORT=8000")
  fi

  args+=("$context")
  podman "${args[@]}"
  ok "$ref"
  echo
done

log "Built:"
podman images --filter "reference=$REGISTRY/$REGISTRY_PROJECT/clear-*:$IMAGE_TAG" \
  --format "table {{.Repository}}:{{.Tag}}\t{{.Size}}\t{{.Created}}"

cat <<EOF

Next:
  ./push-images.sh $IMAGE_TAG
EOF
