#!/usr/bin/env bash
#
# Restart the stack, or one service of it.
#
#   ./restart.sh                       # everything, in order
#   ./restart.sh clear-backend         # one unit
#   ./restart.sh backend               # the prefix is optional
#
# Restarting one service is the common case and is safe: the queue dispatcher and
# both worker pools reconnect, and a stage interrupted by the restart is recovered
# by the stalled-job sweep rather than lost.
#
# This does NOT pick up a new image. It restarts the version the installed unit
# files name. Use ./update.sh for a version change.

. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

require_user_systemd

if [[ $# -gt 0 ]]; then
  unit="$1"
  [[ "$unit" == clear-* ]] || unit="clear-$unit"
  [[ "$unit" == *.service ]] || unit="$unit.service"

  # Fail on a name that does not exist rather than reporting success for a
  # restart that never happened.
  sc cat "$unit" >/dev/null 2>&1 || die "No such unit: $unit
Known units: ${CLEAR_UNITS[*]}"

  log "Restarting $unit"
  sc restart "$unit"
  sleep 3
  sc status "$unit" --no-pager -l | head -n 12
  exit 0
fi

"$SCRIPT_DIR/stop.sh"
echo
exec "$SCRIPT_DIR/start.sh"
