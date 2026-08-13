#!/usr/bin/env bash
#
# Shared helpers. Sourced by every other script here; not executable on its own.
#
#   . "$(dirname "$0")/lib.sh"
#
# It resolves one thing above all: which images this deployment is running. That
# lives in a file rather than in the scripts, because it is the only state that
# distinguishes "the deployment" from "the repository" - and rollback is nothing
# more than putting the previous value of IMAGE_TAG back.

set -euo pipefail

# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
# Colour only when stdout is a terminal, so `./health-check.sh > report.txt` and
# journald both stay readable.
if [[ -t 1 ]]; then
  C_BLUE=$'\033[1;34m'; C_GREEN=$'\033[1;32m'; C_YELLOW=$'\033[1;33m'
  C_RED=$'\033[1;31m';  C_DIM=$'\033[2m';      C_OFF=$'\033[0m'
else
  C_BLUE=''; C_GREEN=''; C_YELLOW=''; C_RED=''; C_DIM=''; C_OFF=''
fi

log()  { printf '%s==>%s %s\n' "$C_BLUE" "$C_OFF" "$*"; }
ok()   { printf '%s ok %s %s\n' "$C_GREEN" "$C_OFF" "$*"; }
warn() { printf '%s !! %s %s\n' "$C_YELLOW" "$C_OFF" "$*" >&2; }
die()  { printf '%s xx %s %s\n' "$C_RED" "$C_OFF" "$*" >&2; exit 1; }
dim()  { printf '%s%s%s\n' "$C_DIM" "$*" "$C_OFF"; }

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# deploy/podman
DEPLOY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
# repository root
REPO_ROOT="$(cd "$DEPLOY_DIR/../.." && pwd)"

# Where the deployment's own configuration lives on the VM. Overridable so a
# second stack can run on one host for a cutover.
CLEAR_CONF_DIR="${CLEAR_CONF_DIR:-/etc/clear}"
IMAGES_ENV="${IMAGES_ENV:-$CLEAR_CONF_DIR/images.env}"

# ---------------------------------------------------------------------------
# Image coordinates
# ---------------------------------------------------------------------------
# Defaults exist so build-images.sh runs on a developer machine that has no
# /etc/clear at all. On the VM the file always wins.
#
# REQUIRES CONFIRMATION: REGISTRY and REGISTRY_PROJECT have no defensible default.
# Set them in the environment, or in images.env, before the first build.
: "${REGISTRY:=}"
: "${REGISTRY_PROJECT:=}"
: "${IMAGE_TAG:=}"
: "${PREVIOUS_IMAGE_TAG:=}"

# Third-party images. Pinned by digest is better still; pinned by tag is the
# minimum. `:latest` on a production host means a `podman pull` months from now
# quietly installs a different major version.
: "${REDIS_IMAGE:=docker.io/library/redis:7-alpine}"

# The single env file every container is started with. Every script reads it and
# every unit references it, so losing this default makes `set -u` abort the whole
# suite with `ENV_FILE: unbound variable` - a failure that names none of them.
: "${ENV_FILE:=$CLEAR_CONF_DIR/clear.env}"
: "${INSTALL_DIR:=$DEPLOY_DIR}"

# Documents live on a podman volume mounted here in the backend and both worker
# pools. Kept in one place because the health check, the backup and three unit
# files all have to agree on it, and STORAGE_LOCAL_ROOT in clear.env has to match.
: "${STORAGE_LOCAL_ROOT:=/var/lib/cip/storage}"
: "${STORAGE_VOLUME:=clear-storage}"

load_images_env() {
  if [[ -f "$IMAGES_ENV" ]]; then
    # shellcheck disable=SC1090
    . "$IMAGES_ENV"
  fi
}

require_image_coordinates() {
  [[ -n "$REGISTRY" ]]         || die "REGISTRY is not set. Put it in $IMAGES_ENV or export it."
  [[ -n "$REGISTRY_PROJECT" ]] || die "REGISTRY_PROJECT is not set. Put it in $IMAGES_ENV or export it."
  [[ -n "$IMAGE_TAG" ]]        || die "IMAGE_TAG is not set. Put it in $IMAGES_ENV or export it."
}

# ---------------------------------------------------------------------------
# The images this repository produces
# ---------------------------------------------------------------------------
# Three, not four. The backend image is also the worker image and the migration
# image: identical code, different entrypoint. Building a second copy of the same
# 2.5 GB would add a way for the API and the workers to drift apart between
# builds, which is the one failure this architecture is arranged to prevent - the
# workers execute pipeline stages by importing the modules the API imports.
#
#   name : build context : dockerfile : target
APP_IMAGES=(
  "clear-backend:backend:backend/Dockerfile:runtime"
  "clear-queue:queue:queue/Dockerfile:runtime"
  "clear-frontend:frontend:frontend/Dockerfile:runtime"
)

image_ref() { printf '%s/%s/%s:%s' "$REGISTRY" "$REGISTRY_PROJECT" "$1" "${2:-$IMAGE_TAG}"; }

# ---------------------------------------------------------------------------
# Systemd units, in start order
# ---------------------------------------------------------------------------
# Referenced by start/stop/restart/health-check. Order matters for start; stop
# walks it backwards.
CLEAR_UNITS=(
  clear-redis.service
  clear-migrate.service
  clear-backend.service
  clear-worker-parser.service
  clear-worker-ai.service
  clear-queue.service
  clear-frontend.service
)

# Long-running containers only - clear-migrate is a oneshot and has no container
# to inspect once it has exited.
CLEAR_CONTAINERS=(
  clear-redis
  clear-backend
  clear-worker-parser
  clear-worker-ai
  clear-queue
  clear-frontend
)

# ---------------------------------------------------------------------------
# Preconditions
# ---------------------------------------------------------------------------
require_podman() {
  command -v podman >/dev/null 2>&1 || die "podman is not installed or not on PATH."
}

# Every unit here is a *user* unit, run by the unprivileged account that owns the
# deployment. `systemctl --user` against the wrong session is the most common way
# these scripts appear to do nothing at all.
require_user_systemd() {
  command -v systemctl >/dev/null 2>&1 || die "systemctl is not available."
  systemctl --user show-environment >/dev/null 2>&1 || die \
"No systemd user session for $(id -un).

Over SSH this usually means linger is off, so the user manager exits with the
login session and takes every container with it:

    sudo loginctl enable-linger $(id -un)

Then log out and back in."
}

sc() { systemctl --user "$@"; }
