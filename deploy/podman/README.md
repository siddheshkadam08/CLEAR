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
| `systemd/` | plain units for the two run-once jobs (migrate, bucket provisioning). Installed to `~/.config/systemd/user/`. |
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
their own images. Redis and MinIO are upstream images, pulled unmodified.
