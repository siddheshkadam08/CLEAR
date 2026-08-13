#!/usr/bin/env bash
#
# Authenticate podman to the container registry. Runs on the build machine and on
# the production VM.
#
#   ./login-registry.sh                       # prompts for the password
#   REGISTRY_USER=deploy REGISTRY_PASSWORD=... ./login-registry.sh   # CI
#
# The credential is stored in ${XDG_RUNTIME_DIR}/containers/auth.json, owned by
# and readable only by the account that ran this. On the VM that is the
# unprivileged deployment user, not root.
#
# Note for the VM: XDG_RUNTIME_DIR is per-login-session. A token written by an
# interactive SSH session is NOT visible to the systemd user manager if linger
# was enabled afterwards, which is one way `podman pull` succeeds by hand and
# then fails from a unit. --authfile below pins it somewhere both can read.

. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

load_images_env
require_podman
[[ -n "$REGISTRY" ]] || die "REGISTRY is not set. Put it in $IMAGES_ENV or export it."

# A stable location, so an image pulled by hand and an image pulled by a systemd
# unit use the same credential. podman reads REGISTRY_AUTH_FILE without needing
# --authfile on every later command.
AUTH_FILE="${REGISTRY_AUTH_FILE:-$HOME/.config/containers/auth.json}"
mkdir -p "$(dirname "$AUTH_FILE")"

log "Registry: $REGISTRY"
log "Auth file: $AUTH_FILE"

if [[ -n "${REGISTRY_PASSWORD:-}" ]]; then
  [[ -n "${REGISTRY_USER:-}" ]] || die "REGISTRY_PASSWORD is set but REGISTRY_USER is not."
  # --password-stdin, never --password: an argument is visible in `ps` to every
  # user on the box for as long as the command runs.
  printf '%s' "$REGISTRY_PASSWORD" |
    podman login --username "$REGISTRY_USER" --password-stdin \
                 --authfile "$AUTH_FILE" "$REGISTRY"
else
  podman login --authfile "$AUTH_FILE" "$REGISTRY"
fi

ok "Logged in to $REGISTRY."

cat <<EOF

Make it the default for every later podman command in this shell, and for the
systemd units, by exporting it in the deployment user's profile:

  echo 'export REGISTRY_AUTH_FILE=$AUTH_FILE' >> ~/.bashrc

Verify the credential actually works, rather than only that login exited 0:

  podman search --limit 1 $REGISTRY/ >/dev/null 2>&1 || \\
    podman manifest inspect $REGISTRY/${REGISTRY_PROJECT:-<project>}/clear-backend:${IMAGE_TAG:-<tag>} >/dev/null
EOF
