#!/usr/bin/env bash
#
# Is this deployment actually working?
#
#   ./health-check.sh            # one pass, exits non-zero if anything is wrong
#   ./health-check.sh --wait     # poll until healthy or a timeout (used by start.sh)
#
# It checks the things that can be red while `podman ps` is entirely green:
#
#   * /readyz, not just /healthz. Liveness deliberately checks nothing external,
#     so a backend that cannot reach Postgres or has no pgvector extension answers
#     /healthz with 200 all day. /readyz is the one that looks.
#   * The dependency edges the containers own but systemd does not: backend to
#     Postgres, worker to Redis, backend and both workers to the storage volume,
#     backend to the extractor.
#   * Queue depth. A dispatcher that is up but not draining looks identical to a
#     healthy one from the outside, and the symptom users report is "uploads never
#     finish".

. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

load_images_env
require_podman

WAIT=0
[[ "${1:-}" == "--wait" ]] && WAIT=1
DEADLINE=$(( SECONDS + ${HEALTH_TIMEOUT:-240} ))

FAILURES=0
pass() { printf '  %s ok  %s %s\n' "$C_GREEN" "$C_OFF" "$*"; }
fail() { printf '  %s FAIL%s %s\n' "$C_RED" "$C_OFF" "$*"; FAILURES=$((FAILURES+1)); }
info() { printf '  %s --  %s %s\n' "$C_DIM" "$C_OFF" "$*"; }

# `curl -fsS`, never a bare curl: a plain curl exits 0 on a 404, so a check
# written without -f reports every wrong URL as healthy. Run inside the container
# so the result does not depend on which ports happen to be published.
in_container() { podman exec "$1" sh -c "$2" >/dev/null 2>&1; }

run_checks() {
  FAILURES=0

  # ---- systemd -------------------------------------------------------------
  log "systemd units"
  for unit in "${CLEAR_UNITS[@]}"; do
    state="$(systemctl --user is-active "$unit" 2>/dev/null || true)"
    case "$state" in
      # clear-migrate is a oneshot and sits at active/exited having done its
      # work, which is success, not a stopped service.
      active) pass "$(printf '%-28s %s' "$unit" "$state")" ;;
      *)      fail "$(printf '%-28s %s' "$unit" "${state:-unknown}")" ;;
    esac
  done

  # ---- containers ----------------------------------------------------------
  echo
  log "containers"
  for name in "${CLEAR_CONTAINERS[@]}"; do
    if ! podman container exists "$name" 2>/dev/null; then
      fail "$(printf '%-22s %s' "$name" "does not exist")"
      continue
    fi
    status="$(podman inspect --format '{{.State.Status}}' "$name" 2>/dev/null || echo unknown)"
    health="$(podman inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$name" 2>/dev/null || echo none)"
    if [[ "$status" == "running" && ( "$health" == "healthy" || "$health" == "none" ) ]]; then
      pass "$(printf '%-22s %-9s %s' "$name" "$status" "$health")"
    else
      fail "$(printf '%-22s %-9s %s' "$name" "$status" "$health")"
    fi
  done

  # ---- HTTP ----------------------------------------------------------------
  echo
  log "endpoints"

  if in_container clear-backend "curl -fsS http://127.0.0.1:8000/healthz"; then
    pass "backend  /healthz   (liveness)"
  else
    fail "backend  /healthz   (liveness)"
  fi

  # The one that matters. 503 here with a green /healthz means a dependency is
  # down; the body names which.
  if in_container clear-backend "curl -fsS http://127.0.0.1:8000/readyz"; then
    pass "backend  /readyz    (database, extensions, storage)"
  else
    fail "backend  /readyz    (database, extensions, storage)"
    if podman container exists clear-backend; then
      podman exec clear-backend sh -c "curl -sS http://127.0.0.1:8000/readyz" 2>/dev/null |
        head -c 800 | sed 's/^/       /' || true
      echo
    fi
  fi

  for pair in "clear-worker-parser:parser" "clear-worker-ai:ai"; do
    IFS=: read -r cname role <<<"$pair"
    if in_container "$cname" "curl -fsS http://127.0.0.1:8001/healthz"; then
      pass "worker   /healthz   (role=$role)"
    else
      fail "worker   /healthz   (role=$role)"
    fi
  done

  if in_container clear-queue "wget --quiet --spider http://127.0.0.1:9100/healthz"; then
    pass "queue    /healthz   (dispatcher)"
  else
    fail "queue    /healthz   (dispatcher)"
  fi

  if in_container clear-frontend "wget --quiet --spider http://127.0.0.1:8080/health"; then
    pass "frontend /health    (nginx)"
  else
    fail "frontend /health    (nginx)"
  fi

  # The proxy hop the browser actually takes. A frontend that serves its own
  # /health while /api/ 502s is the single most common broken deployment, and it
  # looks entirely healthy from every check above.
  if in_container clear-frontend "wget --quiet --spider http://127.0.0.1:8080/api/v1/auth/methods"; then
    pass "frontend /api/ -> backend  (proxy hop)"
  else
    fail "frontend /api/ -> backend  (proxy hop)"
  fi

  # ---- dependencies --------------------------------------------------------
  echo
  log "dependencies"

  if in_container clear-redis "redis-cli ping"; then
    pass "redis    PING"
  else
    fail "redis    PING"
  fi

  # From the backend's own network namespace, with the backend's own DSN - which
  # is the only test that proves the containers can reach the host's Postgres.
  if podman exec clear-backend python -c "
import asyncio, sys
from app.db.session import database_healthy
sys.exit(0 if asyncio.run(database_healthy()) else 1)" >/dev/null 2>&1; then
    pass "postgres reachable from the backend container"
  else
    fail "postgres reachable from the backend container"
    info "the host firewall, listen_addresses, pg_hba.conf and POSTGRES_HOST are the four candidates"
  fi

  # Document storage is a mounted filesystem, not a service, so "is it up?" is the
  # wrong question. The two that matter are whether the volume is actually mounted
  # and whether the runtime user can write to it - a container that lost its mount
  # keeps working perfectly until the first upload, then writes into its own
  # writable layer and loses the file on the next recreate, with `storage_path` in
  # the database still pointing at it.
  if in_container clear-backend "test -d $STORAGE_LOCAL_ROOT"; then
    pass "storage  $STORAGE_LOCAL_ROOT present in the backend container"
  else
    fail "storage  $STORAGE_LOCAL_ROOT present in the backend container"
    info "check Volume=clear-storage.volume:$STORAGE_LOCAL_ROOT in clear-backend.container"
  fi

  # Written and removed as the container's own unprivileged user, which is the only
  # thing that proves the mount is writable BY THE PROCESS. A root-owned volume
  # directory is the common failure and it looks identical from outside.
  if in_container clear-backend \
       "touch $STORAGE_LOCAL_ROOT/.healthcheck && rm -f $STORAGE_LOCAL_ROOT/.healthcheck"; then
    pass "storage  writable by the backend's runtime user"
  else
    fail "storage  writable by the backend's runtime user"
    info "podman exec clear-backend ls -ld $STORAGE_LOCAL_ROOT   # expect owner cip (uid 10001)"
  fi

  # The workers write the converted PDF the API later streams, so a mount that is
  # present in the API and missing in a worker produces documents that upload fine
  # and then fail mid-pipeline.
  for cname in clear-worker-parser clear-worker-ai; do
    if in_container "$cname" \
         "touch $STORAGE_LOCAL_ROOT/.healthcheck-$cname && rm -f $STORAGE_LOCAL_ROOT/.healthcheck-$cname"; then
      pass "storage  writable by $cname"
    else
      fail "storage  writable by $cname"
    fi
  done

  # The parser. Reached by CONTAINER name on the podman network - the host's
  # 127.0.0.1:58001 does not exist from in here.
  if in_container clear-backend "curl -fsS http://clear-extractor:8000/health"; then
    pass "extractor reachable at clear-extractor:8000 from the backend"
  else
    fail "extractor reachable at clear-extractor:8000 from the backend"
    info "on the host the same service answers at http://127.0.0.1:58001/health;"
    info "if that works and this does not, the extractor is not on the clear-net network"
  fi

  # ---- pipeline ------------------------------------------------------------
  echo
  log "pipeline"

  # Which stages actually have a handler. A stage whose module failed to import -
  # a missing optional extra, usually - truncates every job at that point, and the
  # only other evidence is contracts that stop advancing.
  if stages="$(podman exec clear-backend python -m app.cli stages 2>/dev/null)"; then
    if grep -qi "unavailable\|error" <<<"$stages"; then
      fail "some pipeline stages have no handler"
      sed 's/^/       /' <<<"$stages"
    else
      pass "all pipeline stages have a handler"
    fi
  else
    info "could not run 'cli stages' (backend not up yet?)"
  fi

  return $FAILURES
}

if (( WAIT )); then
  while true; do
    if run_checks; then
      echo
      ok "Healthy."
      exit 0
    fi
    if (( SECONDS >= DEADLINE )); then
      echo
      die "Still unhealthy after ${HEALTH_TIMEOUT:-240}s. $FAILURES check(s) failing.

  journalctl --user -u clear-backend.service -n 80 --no-pager
  podman logs --tail 80 clear-backend"
    fi
    echo
    dim "retrying in 10s ($(( DEADLINE - SECONDS ))s left)"
    sleep 10
    echo
  done
fi

# `|| true`, because run_checks returns the failure count and `set -e` would take
# a bare non-zero return as the end of the script - exiting silently, without the
# summary, from the one command an operator runs to find out what is wrong.
run_checks || true
echo
if (( FAILURES == 0 )); then
  ok "Healthy."
else
  die "$FAILURES check(s) failing."
fi
