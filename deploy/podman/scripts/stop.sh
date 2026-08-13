#!/usr/bin/env bash
#
# Stop the stack, in reverse dependency order.
#
#   ./stop.sh
#
# Reverse order matters: stopping the workers before the frontend would leave the
# site up and accepting uploads that nothing will process. Taking the ingress away
# first makes the outage honest.
#
# Nothing is deleted. Volumes, the database and the images are untouched, and
# ./start.sh brings the same version back.

. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

require_user_systemd

log "Stopping units in reverse dependency order"

# The units carry TimeoutStopSec values of 30-300s: the AI worker pool is given
# five minutes because a stage in flight is minutes of paid provider calls, and
# killing it means paying for them again. This will not be instant, and should
# not be.
for (( i=${#CLEAR_UNITS[@]}-1 ; i>=0 ; i-- )); do
  unit="${CLEAR_UNITS[$i]}"
  printf '  %-34s' "$unit"
  if sc stop "$unit" >/dev/null 2>&1; then
    printf '%sstopped%s\n' "$C_GREEN" "$C_OFF"
  else
    printf '%s(was not running)%s\n' "$C_DIM" "$C_OFF"
  fi
done

echo
ok "Stopped. Volumes, database and images are untouched."
dim "Start again:   $SCRIPT_DIR/start.sh"
