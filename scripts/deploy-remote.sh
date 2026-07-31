#!/usr/bin/env bash
#
# Ship this working tree to a server and run scripts/deploy.sh there.
#
#   ./scripts/deploy-remote.sh irisdev@13.234.93.157 [-i ~/.ssh/key.pem] [--fresh]
#
# Copies a tar of the tracked tree rather than using rsync (absent on Windows) or
# git push (the target has no repository). Excludes node_modules, .venv, dist and
# .env: the first three are rebuilt on the server and the last belongs to the
# server alone - copying a local .env would overwrite the deployment's generated
# secrets with development ones.
#
set -euo pipefail

cd "$(dirname "$0")/.."

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31mxx\033[0m  %s\n' "$*" >&2; exit 1; }

TARGET="${1:-}"
[[ -n "$TARGET" ]] || die "Usage: $0 user@host [-i identity_file] [--fresh] [--path /remote/dir]"
shift

SSH_OPTS=()
DEPLOY_ARGS=()
REMOTE_DIR="~/clear"
# The address a browser will use. Defaults to the host you are SSHing to, which is
# almost always right and is certainly better than the server's own `hostname -I`:
# on a cloud VM that returns the private address, and every presigned download URL
# would then be signed for an address no laptop can reach.
PUBLIC_HOST="${TARGET#*@}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    -i)             SSH_OPTS+=(-i "$2");   shift 2 ;;
    --path)         REMOTE_DIR="$2";       shift 2 ;;
    --public-host)  PUBLIC_HOST="$2";      shift 2 ;;
    --fresh)        DEPLOY_ARGS+=(--fresh); shift ;;
    *)              die "Unknown argument: $1" ;;
  esac
done

command -v ssh >/dev/null 2>&1 || die "ssh is not available."
command -v tar >/dev/null 2>&1 || die "tar is not available."

log "Checking access to ${TARGET}."
ssh "${SSH_OPTS[@]}" -o BatchMode=yes -o ConnectTimeout=10 "$TARGET" 'echo ok' >/dev/null \
  || die "Cannot authenticate to ${TARGET}. Pass a key with -i, or add yours to the server's authorized_keys."

log "Packaging the working tree."
ARCHIVE="$(mktemp -t clear-deploy-XXXXXX.tar.gz)"
trap 'rm -f "$ARCHIVE"' EXIT

# --exclude before the path, and .env excluded deliberately - see the header.
tar -czf "$ARCHIVE" \
  --exclude='./.git' \
  --exclude='./.env' \
  --exclude='./.env.local' \
  --exclude='*/node_modules' \
  --exclude='*/.venv' \
  --exclude='*/dist' \
  --exclude='*/__pycache__' \
  --exclude='*.pyc' \
  --exclude='./frontend/.vite' \
  --exclude='./backend/.pytest_cache' \
  --exclude='./backend/.mypy_cache' \
  --exclude='./backend/.ruff_cache' \
  -C . .

log "Copying $(du -h "$ARCHIVE" | cut -f1) to ${TARGET}:${REMOTE_DIR}."
ssh "${SSH_OPTS[@]}" "$TARGET" "mkdir -p ${REMOTE_DIR}"
scp "${SSH_OPTS[@]}" "$ARCHIVE" "${TARGET}:${REMOTE_DIR}/deploy.tar.gz" >/dev/null

log "Unpacking and deploying (public host: ${PUBLIC_HOST})."
ssh "${SSH_OPTS[@]}" "$TARGET" "
  set -e
  cd ${REMOTE_DIR}
  tar -xzf deploy.tar.gz
  rm -f deploy.tar.gz
  chmod +x scripts/deploy.sh
  PUBLIC_HOST='${PUBLIC_HOST}' ./scripts/deploy.sh ${DEPLOY_ARGS[*]:-}
"
