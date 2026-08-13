#!/usr/bin/env bash
#
# Render the unit templates and install them for the current user. Production VM.
#
#   ./install-quadlet.sh
#
# The units in deploy/podman/{quadlet,systemd} are templates: they carry
# @REGISTRY@, @TAG@ and friends where a concrete value belongs. Neither Quadlet
# nor systemd expands environment variables in Image= or ExecStart=, so the values
# are substituted here, at install time.
#
# That is not a workaround, it is the mechanism rollback depends on: the INSTALLED
# unit file states, in plain text, exactly which image tag this host is running.
# `grep Image= ~/.config/containers/systemd/*.container` answers "what is
# deployed?" without asking the registry, the running containers or anyone's
# memory. update.sh and rollback.sh do nothing more than re-run this script with a
# different IMAGE_TAG.
#
# Re-running it is safe and is the normal way to change wiring.

. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

load_images_env
require_image_coordinates
require_podman
require_user_systemd

# Defaulted here rather than in lib.sh, because it is composed from three values
# that are only guaranteed to be set after require_image_coordinates has run.
# Setting EXTRACTOR_IMAGE in images.env overrides this and pins the extractor
# independently of the CLEAR release, which is usually what you want once the two
# stop being cut at the same time.
: "${EXTRACTOR_IMAGE:=$REGISTRY/$REGISTRY_PROJECT/clear-extractor:$IMAGE_TAG}"

QUADLET_DIR="$HOME/.config/containers/systemd"
UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$QUADLET_DIR" "$UNIT_DIR"

[[ -f "$ENV_FILE" ]] || die "$ENV_FILE does not exist. Copy $DEPLOY_DIR/.env.production.example to it and fill it in."

# The unit files reference it by absolute path and systemd reads it as the
# deployment user. A file root can read and this user cannot produces "Failed to
# load environment files", which does not name the permission.
[[ -r "$ENV_FILE" ]] || die "$ENV_FILE is not readable by $(id -un)."

render() {
  local src="$1" dest="$2"
  sed \
    -e "s|@REGISTRY@|$REGISTRY|g" \
    -e "s|@PROJECT@|$REGISTRY_PROJECT|g" \
    -e "s|@TAG@|$IMAGE_TAG|g" \
    -e "s|@REDIS_IMAGE@|$REDIS_IMAGE|g" \
    -e "s|@EXTRACTOR_IMAGE@|$EXTRACTOR_IMAGE|g" \
    -e "s|@ENV_FILE@|$ENV_FILE|g" \
    -e "s|@STORAGE_LOCAL_ROOT@|$STORAGE_LOCAL_ROOT|g" \
    -e "s|@INSTALL_DIR@|$INSTALL_DIR|g" \
    "$src" > "$dest"

  # A placeholder that survives means a new one was added to a template and not
  # to the sed list above - and it would fail at runtime as an unresolvable image
  # name, hours later.
  if grep -q '@[A-Z_]\{2,\}@' "$dest"; then
    grep -n '@[A-Z_]\{2,\}@' "$dest" >&2
    die "Unsubstituted placeholder left in $dest."
  fi
}

log "Rendering units"
log "  registry  $REGISTRY/$REGISTRY_PROJECT"
log "  tag       $IMAGE_TAG"
log "  env file  $ENV_FILE"
log "  storage   volume $STORAGE_VOLUME -> $STORAGE_LOCAL_ROOT"
log "  extractor $EXTRACTOR_IMAGE"
echo

for src in "$DEPLOY_DIR"/quadlet/*.network "$DEPLOY_DIR"/quadlet/*.volume "$DEPLOY_DIR"/quadlet/*.container; do
  [[ -e "$src" ]] || continue
  dest="$QUADLET_DIR/$(basename "$src")"
  render "$src" "$dest"
  ok "$dest"
done

for src in "$DEPLOY_DIR"/systemd/*.service; do
  [[ -e "$src" ]] || continue
  dest="$UNIT_DIR/$(basename "$src")"
  render "$src" "$dest"
  ok "$dest"
done

echo
log "Reloading the user manager"
# This is what runs Quadlet: it reads ~/.config/containers/systemd on every
# daemon-reload and regenerates the .service units. Skipping it means the new
# unit files exist and systemd is still running the previous generation.
sc daemon-reload

# A Quadlet file with a syntax error is silently skipped rather than reported, so
# the generated service simply does not exist - and `systemctl start` then fails
# with "Unit clear-backend.service not found", which reads like a typo.
log "Checking that every unit was generated"
missing=()
for unit in "${CLEAR_UNITS[@]}"; do
  sc cat "$unit" >/dev/null 2>&1 || missing+=("$unit")
done
if (( ${#missing[@]} )); then
  warn "Not generated: ${missing[*]}"
  dim "Quadlet reports parse errors to the journal, not to daemon-reload:"
  dim "  journalctl --user -t quadlet-generator -n 50 --no-pager"
  die "Fix the unit files and re-run."
fi
ok "All ${#CLEAR_UNITS[@]} units generated."

echo
log "Enabling at boot"

# Only the PLAIN unit is enabled here. A Quadlet-generated unit cannot be:
#
#     Failed to enable unit: Unit clear-backend.service is transient or generated.
#
# It has no file in a unit search path - the generator writes it into
# /run/user/<uid>/systemd/generator/ on every daemon-reload. Quadlet reads the
# [Install] section of the .container file itself and creates the
# default.target.wants symlink, so the rest are already enabled by virtue of
# carrying `WantedBy=default.target`. Looping over all of them here would print a
# row of failures that mean nothing and hide the one that would matter.
for unit in clear-migrate.service; do
  sc enable "$unit" >/dev/null 2>&1 && ok "enabled $unit" || warn "Could not enable $unit"
done

# Reported rather than asserted. `is-enabled` answers `generated` for a Quadlet
# unit whether or not its [Install] section produced a default.target.wants
# symlink, so a green result here is not proof that the stack returns after a
# reboot. Printing the states catches the obvious failure - a unit reporting
# `disabled` or `masked` - and the only real verification is the reboot test in
# PODMAN_DEPLOYMENT.md §5 step 37.
log "Boot-time state (the reboot test in the guide is what actually proves this)"
for unit in "${CLEAR_UNITS[@]}"; do
  state="$(sc is-enabled "$unit" 2>/dev/null || echo unknown)"
  case "$state" in
    enabled|generated|static) printf '  %-30s %s\n' "$unit" "$state" ;;
    *)                        warn "$(printf '%-30s %s' "$unit" "$state")" ;;
  esac
done

# Without linger the user manager stops at logout and takes every container with
# it - the stack dies the moment the SSH session that started it closes, and
# nothing survives a reboot.
if ! loginctl show-user "$(id -un)" 2>/dev/null | grep -q 'Linger=yes'; then
  warn "Linger is OFF for $(id -un). Containers will stop at logout and will NOT
     start on boot. Fix it with:

         sudo loginctl enable-linger $(id -un)"
else
  ok "Linger is on; the stack survives logout and reboot."
fi

cat <<EOF

Installed. Start it with:

  $INSTALL_DIR/scripts/start.sh

What is deployed, at any time:

  grep -h '^Image=' ~/.config/containers/systemd/*.container
EOF
