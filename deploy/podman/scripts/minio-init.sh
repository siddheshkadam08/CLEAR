#!/usr/bin/env bash
#
# Create the object-storage bucket the application writes to. Idempotent; run by
# clear-minio-init.service on every boot.
#
# One `podman run`, not several: `mc alias set` writes its configuration into the
# container's own filesystem, which --rm discards. A second `podman run` would
# start with no alias and fail with "Unable to find alias `local`".

. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

load_images_env
require_podman

[[ -f "$MINIO_ENV_FILE" ]] || die "$MINIO_ENV_FILE does not exist. Create it - see PODMAN_DEPLOYMENT.md §5 step 16."

# Read rather than sourced: the file is written for podman --env-file, and a
# stray character in the secret must not become a command here.
MINIO_ROOT_USER="$(sed -n 's/^MINIO_ROOT_USER=//p' "$MINIO_ENV_FILE" | tail -n 1)"
MINIO_ROOT_PASSWORD="$(sed -n 's/^MINIO_ROOT_PASSWORD=//p' "$MINIO_ENV_FILE" | tail -n 1)"
[[ -n "$MINIO_ROOT_USER" && -n "$MINIO_ROOT_PASSWORD" ]] ||
  die "MINIO_ROOT_USER / MINIO_ROOT_PASSWORD are missing from $MINIO_ENV_FILE."

log "Provisioning bucket '$STORAGE_BUCKET' on clear-minio"

# The retry loop is inside the container because that is where the DNS name
# clear-minio resolves. MinIO accepts connections a second or two after the
# container starts, and `mc` reports that as a plain connection refusal.
podman run --rm --replace --name clear-minio-init \
  --network clear-net \
  --entrypoint /bin/sh \
  "$MC_IMAGE" -c '
    set -e
    user=$1; pass=$2; bucket=$3
    for attempt in $(seq 1 60); do
      if mc alias set local "http://clear-minio:9000" "$user" "$pass" >/dev/null 2>&1; then
        mc mb --ignore-existing "local/$bucket"
        # Belt and braces: MinIO creates buckets private, and a public one would
        # make every uploaded contract readable by URL alone - defeating the
        # presigned URLs the application goes to the trouble of generating.
        # Non-fatal because the subcommand was `mc policy` before mc RELEASE
        # 2023-01, and an older client here must not fail the whole unit.
        mc anonymous set none "local/$bucket" >/dev/null 2>&1 ||
          mc policy set none "local/$bucket" >/dev/null 2>&1 ||
          echo "note: could not assert the bucket policy; MinIO defaults it to private" >&2
        echo "bucket ready: $bucket"
        exit 0
      fi
      sleep 2
    done
    echo "MinIO did not accept a connection within 120s" >&2
    exit 1
  ' mc-init "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" "$STORAGE_BUCKET"

ok "Bucket '$STORAGE_BUCKET' present."
