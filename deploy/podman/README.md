# `deploy/podman`

Production deployment: images built elsewhere, pushed to a registry, pulled and
run on a VM under rootless Podman + systemd. PostgreSQL is installed on the VM and
is not containerised.

**The guide is [`docs/PODMAN_DEPLOYMENT.md`](../../docs/PODMAN_DEPLOYMENT.md).**
This file is only an index of what is here.

## Layout

| Path | What it is |
|---|---|
| `.env.production.example` | every setting the deployment reads, annotated. Copy to `/etc/clear/clear.env`. |
| `quadlet/` | Podman Quadlet units for the seven long-running containers, plus the network and three volumes. Installed to `~/.config/containers/systemd/`. |
| `systemd/` | plain unit for the run-once migration job. Installed to `~/.config/systemd/user/`. |
| `nginx/clear.conf` | the host's nginx: TLS on 443, proxy to the frontend container on `127.0.0.1:8080`. |
| `scripts/` | build, push, pull, install, start, stop, update, rollback, health-check, backup. |

## The files here are templates

`quadlet/` and `systemd/` carry `@REGISTRY@`, `@TAG@` and similar placeholders,
because neither Quadlet nor systemd expands environment variables in `Image=` or
`ExecStart=`. `scripts/install-quadlet.sh` substitutes them at install time and
refuses to finish if any placeholder survives.

That is what makes the deployed state legible and rollback mechanical — the
installed unit file names the running image tag in plain text:

```bash
grep -h '^Image=' ~/.config/containers/systemd/*.container
```

## Two-minute version

```bash
# build machine
export REGISTRY=<registry> REGISTRY_PROJECT=<org>/clear
./scripts/login-registry.sh
./scripts/build-images.sh v1.0.0
./scripts/push-images.sh  v1.0.0

# VM, first time
sudo ./scripts/install-postgres.sh --password "$(openssl rand -base64 24 | tr -d '/+=')"
sudo -u clear -i
cp .env.production.example /etc/clear/clear.env && chmod 600 /etc/clear/clear.env
$EDITOR /etc/clear/clear.env                    # every "<<< SET THIS" line
./scripts/login-registry.sh
./scripts/pull-images.sh
./scripts/install-quadlet.sh
./scripts/start.sh

# VM, every release after that
./scripts/update.sh v1.0.1                      # health-gated, rolls back on failure
```

## Three images, not five

`clear-backend` is also the worker image and the migration image — identical code,
different entrypoints, four separate containers. The worker pools execute pipeline
stages by importing the modules the API imports, so a second image would be ~2.5 GB
duplicated and a way for the two to drift apart between builds.

`clear-queue` and `clear-frontend` are genuinely separate deployables and have
their own images. Redis is an upstream image, pulled unmodified. The PDF
extractor (`clear-extractor`) is built and released from a separate repository.

## Document storage is the local filesystem

`STORAGE_PROVIDER=local`, `STORAGE_LOCAL_ROOT=/var/lib/cip/storage`. There is no
object store: contract bytes live on the `clear-storage` podman volume, mounted
into the API and both worker pools, and the browser reaches them through
`GET /api/v1/contracts/{contract_id}/content` — already covered by the frontend's
`/api/` proxy, so downloads are same-origin with no second upstream.

That makes `clear-storage` as critical as the database. `documents.storage_path`
is a path *into* it, so the volume and the Postgres dump are one backup: restore
either without the other and the application looks healthy while every download
404s. `scripts/backup.sh` takes both together and refuses to run if the volume is
missing.
