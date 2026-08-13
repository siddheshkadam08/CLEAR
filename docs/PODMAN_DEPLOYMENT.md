# CLEAR — Production deployment on a fresh VM with Podman

Build images on a build machine, push them to a registry, pull them on the VM, run
them with Podman under systemd. PostgreSQL is installed **directly on the VM** and
is never containerised.

Follow §5 top to bottom on a fresh VM and you will not have to guess what to run
next. Everything before it explains what you are about to build and why it is
shaped the way it is; everything after it is operating the thing once it runs.

Every command, port, path, environment variable and health endpoint below was read
out of this repository. Anything that could not be — a hostname, a vendor account,
a registry — is marked **`REQUIRES CONFIRMATION`** and is never silently invented.

---

## 1. What gets deployed

```text
                        ┌──────────────────────────────┐
                        │      Container registry      │
                        │  clear-backend  : <tag>      │
                        │  clear-queue    : <tag>      │
                        │  clear-frontend : <tag>      │
                        └───────────────┬──────────────┘
                                        │  podman pull
┌───────────────────────────────────────┼───────────────────────────────────────┐
│  PRODUCTION VM                        ▼                                       │
│                                                                               │
│   internet ─► nginx (host, :80/:443, TLS)                                     │
│                    │                                                          │
│                    ▼  127.0.0.1:8080                                          │
│              ┌───────────────┐                                                │
│              │ clear-frontend│  nginx: React SPA + /api proxy                 │
│              └───────┬───────┘                                                │
│                      │  clear-net (podman bridge)                             │
│         ┌────────────┼─────────────────┬──────────────────┐                   │
│         ▼            ▼                 ▼                  ▼                   │
│  ┌────────────┐ ┌──────────┐  ┌────────────────┐ ┌────────────────┐          │
│  │clear-backend│ │clear-queue│  │clear-worker-   │ │clear-worker-ai │          │
│  │  API :8000 │ │ BullMQ   │  │parser   :8001  │ │        :8001   │          │
│  └──┬──────┬──┘ │  :9100   │  └──┬─────────────┘ └──┬─────────────┘          │
│     │      │    └────┬─────┘     │                  │                        │
│     │      └─────────┴───────────┴──────────────────┘                        │
│     │                ▼                                                        │
│     │         ┌────────────┐  ┌─────────────────┐                            │
│     │         │clear-redis │  │ clear-extractor │  PDF layout, :8000         │
│     │         │broker+cache│  └─────────────────┘  (host :58001)             │
│     │         └────────────┘                                                  │
│     ▼                                                                         │
│  ┌──────────────────────────────────────────┐                                │
│  │ volume clear-storage                     │  mounted /var/lib/cip/storage  │
│  │ every contract and export, on disk       │  in API + both worker pools    │
│  └──────────────────────────────────────────┘                                │
│                                                                               │
│  ┌─────────────────────────────────────────────────────────────────────────┐ │
│  │  PostgreSQL 17 + pgvector — installed on the VM, a systemd service,      │ │
│  │  data in /var/lib/postgresql. NOT a container. NOT a podman volume.      │ │
│  └─────────────────────────────────────────────────────────────────────────┘ │
└───────────────────────────────────────────────────────────────────────────────┘
```

There is **no object store**. `STORAGE_PROVIDER=local`: document bytes live on the
`clear-storage` podman volume and the browser reaches them through
`GET /api/v1/contracts/{contract_id}/content`, which the frontend's `/api/` proxy
already covers.

### What each service actually is, from the code

| Service | Technology | Entrypoint | Port | Health |
|---|---|---|---|---|
| `clear-backend` | FastAPI, Python 3.11, gunicorn + `uvicorn.workers.UvicornWorker` | `gunicorn app.main:app` | 8000 | `GET /healthz`, `GET /readyz` |
| `clear-worker-parser` | same image | `uvicorn app.worker_app:app --port 8001` | 8001 | `GET /healthz` |
| `clear-worker-ai` | same image | `uvicorn app.worker_app:app --port 8001` | 8001 | `GET /healthz` |
| `clear-queue` | Node 20, BullMQ 5, Express | `node dist/index.js` | 9100 | `GET /healthz` |
| `clear-frontend` | React 18 + Vite 6 + TypeScript, served by nginx 1.27-alpine | nginx | 8080 | `GET /health` |
| `clear-redis` | `redis:7-alpine` | `redis-server --appendonly yes` | 6379 | `redis-cli ping` |
| `clear-extractor` | PDF layout extractor, **built from a separate repository** | its own | 8000 (host `58001`) | `GET /health` |
| PostgreSQL | 17 + pgvector, **on the host** | `systemd` | 5432 | `pg_isready` |

Liveness is `/healthz` **at the root**, not under `/api/v1`. Probe it with
`curl -fsS`, never a bare `curl` — a plain curl exits 0 on a 404, so a check
written without `-f` reports every wrong URL as healthy.

`/docs`, `/redoc` and `/openapi.json` are **disabled when `APP_ENV=production`**
(`Settings.docs_url`). The frontend proxies them, so they will 404 in production.
That is intended.

---

## 2. Decisions that differ from a naive reading, and why

Four things about this deployment are deliberate. Each is a place where doing the
obvious thing would break something.

### 2.1 Three images, not five

The backend image **is** the worker image **and** the migration image — identical
code, three different entrypoints, three separate containers.

The worker pools execute pipeline stages by importing the same modules the API
imports (`app.worker_app` mounts `app.api.internal`, which the API also mounts).
A second image would be a second copy of the same ~2.5 GB of LibreOffice,
Tesseract, Poppler and Python wheels, and — much worse — a way for the API and the
workers to be built from different commits. One image, one digest, one thing to
promote through the registry.

This is not "combining unrelated services into one container": there are four
separate containers running that image, scaled and restarted independently.

### 2.2 Documents are files on a volume, not objects in a store

`STORAGE_PROVIDER=local`, `STORAGE_LOCAL_ROOT=/var/lib/cip/storage`. The bytes of
every contract and every export live on the `clear-storage` podman volume, mounted
into the API and both worker pools.

The browser never touches storage. The local adapter's `signed_url()` has nothing
to sign against, so it returns an API-relative path and `ContractService.file_access`
rewrites it onto `GET /api/v1/contracts/{contract_id}/content` — which streams from
the storage adapter with the request's own authorisation applied. That route is
under `/api/`, which the frontend nginx already proxies, so downloads are
same-origin with no second upstream, no bucket name in the URL space, and no
presigned signature that has to survive a proxy hop intact.

The application supported this all along; the only code change needed was
deleting a production guard that refused `STORAGE_PROVIDER=local` outright.

**This makes `clear-storage` as critical as the database.**
`documents.storage_path` is a path *into* that volume, so the two are one backup
unit — see §12. Losing the volume does not produce an empty-looking application;
it produces a full-looking one where every download 404s.

PostgreSQL is *not* on a podman volume, because the database is the one thing
whose lifecycle must not be coupled to a container's: its data directory belongs
on the VM's filesystem where the distribution's own backup, upgrade and monitoring
tooling can see it, `pg_dump` runs without an `exec` into anything, and a mistyped
`podman volume rm` cannot take it.

### 2.3 One env file per container, with no allow-list

`docker-compose.yml` forwards only the variables named in its `x-backend-env`
block. That block is an **allow-list, not a passthrough**, and this repository's
history is full of the resulting bug: a key set correctly in `.env`, silently
dropped on the way to the container, and a symptom that reads as a code fault —
`OIDC_ENABLED=true` reported as disabled, `PDFEXTRACT_URL` ignored while the
adapter fell back to a subprocess it could not run, `RETRIEVAL_*` tuning that
changed nothing.

This deployment uses `podman --env-file`, which has no allow-list. What is in the
file is what the process sees.

That removes one class of bug and introduces one obligation: the file now reaches
services it did not before. Podman's `--env-file` parser is not a shell —

* **do not quote values.** `CORS_ORIGINS="https://x"` sets the value to
  `"https://x"` *including the quotes*, and the CORS check then never matches.
* **no `$VAR` interpolation.** Write every value out in full.
* **LF line endings only.** A CRLF file leaves a trailing `\r` on every value;
  `DATABASE_URL` then names a host with a carriage return in it and the error
  blames the host. `start.sh` refuses to run on either mistake.

### 2.4 Rootless, under a dedicated user, with a host nginx in front

Containers run as an unprivileged `clear` account, and the processes *inside* them
are already non-root (`cip` uid 10001 in the backend, `node`, `nginx`).

Rootless podman cannot bind ports 80 or 443. Rather than granting that capability,
the frontend publishes on `127.0.0.1:8080` and the host's nginx terminates TLS and
proxies to it. TLS certificates are host state anyway — renewed by certbot, root
readable — and mounting them into a container replaced on every deploy would mean
re-mounting them on every deploy.

---

## 3. Repository layout added by this deployment

```text
deploy/podman/
├── .env.production.example      every setting, annotated, with SET THIS markers
├── README.md
├── quadlet/                     Podman Quadlet units (long-running containers)
│   ├── clear.network
│   ├── clear-storage.volume     THE document store
│   ├── clear-redis-data.volume
│   ├── clear-redis.container
│   ├── clear-backend.container  clear-queue.container
│   ├── clear-worker-parser.container  clear-worker-ai.container
│   └── clear-frontend.container
├── systemd/                     plain unit for the run-once migration
│   └── clear-migrate.service
├── nginx/clear.conf             host TLS reverse proxy
└── scripts/
    ├── lib.sh                shared helpers, image coordinates, unit list
    ├── install-postgres.sh   PostgreSQL 17 + pgvector on the VM        (sudo)
    ├── build-images.sh       build machine
    ├── push-images.sh        build machine
    ├── login-registry.sh     both
    ├── pull-images.sh        VM
    ├── install-quadlet.sh    VM — renders the unit templates
    ├── start.sh  stop.sh  restart.sh
    ├── update.sh  rollback.sh
    ├── health-check.sh
    └── backup.sh
```

The unit files are **templates**: they carry `@REGISTRY@`, `@TAG@` and friends,
because neither Quadlet nor systemd expands environment variables in `Image=` or
`ExecStart=`. `install-quadlet.sh` substitutes them at install time.

That is not a workaround — it is what rollback depends on. The *installed* unit
file states in plain text which image tag this host runs, so

```bash
grep -h '^Image=' ~/.config/containers/systemd/*.container
```

answers "what is deployed?" without asking the registry, the containers, or
anyone's memory.

---

## 4. VM specification

| | Recommended | Minimum |
|---|---|---|
| vCPU | 4 | 2 |
| RAM | 16 GB | 8 GB + 4 GB swap |
| Disk | 100 GB SSD | 60 GB |
| OS | Ubuntu 24.04 LTS / Rocky 9 | any systemd Linux with Podman ≥ 4.4 |

The backend image is large (~2.5 GB: LibreOffice, Tesseract, Poppler) and three
containers run it. Podman ≥ 4.4 is required for Quadlet; ≥ 4.7 is better.

**Egress required** from the VM to: the container registry, the LLM endpoint, the
embedding endpoint, and the document parser (§5 step 20). `EMBEDDING_VERIFY_ON_STARTUP`
is on by default, so the backend **refuses to boot** if the embedding endpoint is
unreachable or its dimension disagrees with the database column — deliberately,
because a wrong dimension degrades every answer silently for as long as it runs.

---

## 5. Fresh VM, start to finish

### Step 1–3 — Provision, connect, identify

```bash
ssh <user>@<vm>
cat /etc/os-release
uname -r
free -h && df -h / && nproc
```

### Step 4 — Update the OS

```bash
# Debian / Ubuntu
sudo apt-get update && sudo apt-get -y upgrade
sudo apt-get install -y curl ca-certificates git rsync openssl postgresql-client

# RHEL family
sudo dnf -y update
sudo dnf -y install curl ca-certificates git rsync openssl
```

### Step 5 — Hostname and DNS

```bash
sudo hostnamectl set-hostname clear-prod
# REQUIRES CONFIRMATION: point your DNS A record at this VM's public address
# before requesting a certificate in step 34.
```

### Step 6 — Install Podman

```bash
# Debian / Ubuntu
sudo apt-get install -y podman uidmap slirp4netns fuse-overlayfs

# RHEL family
sudo dnf -y install podman
```

### Step 7 — Verify Podman

```bash
podman --version          # must be >= 4.4 for Quadlet
podman info --format '{{.Host.Security.Rootless}}'   # expect: true
ls /usr/lib/systemd/user-generators/podman-user-generator   # Quadlet must be present
```

If `podman-user-generator` is missing, Quadlet is not installed and every unit
will silently fail to generate. On some distributions it is in a separate
`podman-quadlet` package.

### Step 8 — Create the deployment user

```bash
sudo useradd --create-home --shell /bin/bash clear

# Without linger the user's systemd manager exits at logout and takes every
# container with it: the stack dies the moment this SSH session closes, and
# nothing starts on boot. This single command is the difference between a
# deployment and a demo.
sudo loginctl enable-linger clear
loginctl show-user clear | grep Linger     # expect: Linger=yes

sudo mkdir -p /opt/clear /etc/clear
sudo chown -R clear:clear /opt/clear /etc/clear
sudo chmod 750 /etc/clear
```

### Step 9 — Put the deployment assets on the VM

Only `deploy/`, `docs/` and the git metadata are needed — the images are built
elsewhere.

```bash
# From the build machine:
rsync -a --exclude node_modules --exclude .venv \
      ./deploy ./docs <user>@<vm>:/tmp/clear-deploy/
# On the VM:
sudo cp -r /tmp/clear-deploy/* /opt/clear/
sudo chown -R clear:clear /opt/clear
sudo chmod +x /opt/clear/deploy/podman/scripts/*.sh
```

### Step 10 — Create the podman network first

Out of order at first glance, and it has to be: the PostgreSQL installer needs the
network's subnet and gateway to write `pg_hba.conf` and `listen_addresses`, and it
cannot read a network that does not exist yet.

```bash
sudo -u clear -i
podman network create clear-net
podman network inspect clear-net --format '{{range .Subnets}}{{.Subnet}} gw {{.Gateway}}{{end}}'
exit
```

Quadlet adopts it later — `clear.network` generates
`podman network create --ignore`, so an existing network is used, not duplicated.

**This network belongs to the `clear` user, and root cannot see it.** Rootless
podman keeps container storage and networks per user, so `sudo podman network ls`
is empty while `sudo -u clear -i podman network ls` shows it. That is why the
installer below takes `--podman-user`.

### Step 11–13 — Install PostgreSQL 17 + pgvector, create the database

The script does all of it: PGDG repository, install, `initdb` where the
distribution does not do it, enable at boot, `listen_addresses`, `pg_hba.conf`,
role, database, extensions, tuning, and verification.

```bash
# Generate the application password first and keep it - you need it twice.
APP_DB_PASSWORD="$(openssl rand -base64 24 | tr -d '/+=')"
echo "$APP_DB_PASSWORD"

sudo /opt/clear/deploy/podman/scripts/install-postgres.sh \
     --password "$APP_DB_PASSWORD" \
     --db clear --user clear_app --major 17 \
     --podman-user clear
```

It prints the `POSTGRES_HOST` and `DATABASE_URL` to paste into `clear.env`.

**It does not open the database to the world.** `listen_addresses` is
`localhost` plus one address, and `pg_hba.conf` gets one `scram-sha-256` rule per
allowed CIDR, scoped to one database and one role. A `0.0.0.0/0` rule anywhere in
the file makes the script abort.

### Step 14 — Verify PostgreSQL and pgvector

```bash
systemctl status postgresql@17-main      # Debian/Ubuntu
# systemctl status postgresql-17         # RHEL family
psql --version

sudo -u postgres psql -d clear -c \
  "SELECT extname, extversion FROM pg_extension ORDER BY extname;"
```

Expect `btree_gin`, `citext`, `pg_trgm`, `plpgsql`, `uuid-ossp`, `vector`.
`/readyz` gates on **vector, pg_trgm, uuid-ossp and citext**; a missing `vector`
is the silent killer — inserts succeed and every similarity search quietly returns
nothing useful.

The install script already round-trips a vector through an HNSW index, which is
the check that matters: `CREATE EXTENSION` succeeding proves the extension is
registered, not that the build works.

```sql
-- to repeat it by hand
CREATE TABLE t (id serial primary key, embedding vector(3));
INSERT INTO t (embedding) VALUES ('[1,2,3]'), ('[4,5,6]');
CREATE INDEX ON t USING hnsw (embedding vector_cosine_ops);
SELECT id FROM t ORDER BY embedding <=> '[1,2,3]' LIMIT 1;
DROP TABLE t;
```

Application tables do not exist yet. Migrations run in step 25.

### Step 15 — Firewall

```bash
# Debian / Ubuntu
sudo ufw default deny incoming
sudo ufw allow 22/tcp
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable
sudo ufw status verbose

# RHEL family
sudo firewall-cmd --permanent --add-service=ssh
sudo firewall-cmd --permanent --add-service=http
sudo firewall-cmd --permanent --add-service=https
sudo firewall-cmd --reload
```

**Only 22, 80 and 443.** Everything else is on `127.0.0.1` or on the podman
network; reach it over an SSH tunnel. Close the same ports in the cloud security
group too — a host firewall is not a substitute for one.

### Step 16 — Production configuration

```bash
sudo -u clear -i
cp /opt/clear/deploy/podman/.env.production.example /etc/clear/clear.env
chmod 600 /etc/clear/clear.env

# Secrets. The production guard refuses to boot on the shipped defaults.
echo "JWT_SECRET=$(openssl rand -hex 32)"
echo "INTERNAL_API_TOKEN=$(openssl rand -hex 32)"

$EDITOR /etc/clear/clear.env
```

Fill in every line marked `<<< SET THIS` and every `<<< REQUIRES CONFIRMATION`.
At minimum: `DATABASE_URL`, `POSTGRES_PASSWORD`, `JWT_SECRET`,
`INTERNAL_API_TOKEN`, `SEED_ADMIN_PASSWORD`, `NEW_USER_DEFAULT_PASSWORD`,
`CORS_ORIGINS`, and the LLM / embedding block.

The storage block needs no credentials at all — `STORAGE_PROVIDER=local` writes to
a mounted filesystem. What it does need is agreement between three places, which
`start.sh` checks before starting anything:

| Setting | Must equal |
|---|---|
| `STORAGE_LOCAL_ROOT` in `clear.env` | the mount path in the backend and both worker units (`/var/lib/cip/storage`) |
| `STORAGE_CONTAINER` in `clear.env` | itself, for ever — it is part of every stored path, so changing it after the first upload strands everything already written |

### Step 17 — Image coordinates

```bash
cat > /etc/clear/images.env <<'EOF'
# REQUIRES CONFIRMATION: your registry and project.
REGISTRY=ghcr.io
REGISTRY_PROJECT=your-org/clear
IMAGE_TAG=v1.0.0
PREVIOUS_IMAGE_TAG=

REDIS_IMAGE=docker.io/library/redis:7-alpine

# Where documents live. STORAGE_LOCAL_ROOT in clear.env must match the second
# value; start.sh refuses to start if they disagree.
STORAGE_VOLUME=clear-storage
STORAGE_LOCAL_ROOT=/var/lib/cip/storage

ENV_FILE=/etc/clear/clear.env
INSTALL_DIR=/opt/clear/deploy/podman
EOF
chmod 640 /etc/clear/images.env
```

### Step 18–19 — Build and push (on the BUILD MACHINE, not the VM)

Do not build on the production VM. The build is minutes of CPU and several
gigabytes of intermediate layers on a host that is meant to be serving — and it
would mean the running version is whatever the working tree happened to contain,
which no registry can attest to and no rollback can return to.

```bash
cd <repo>
export REGISTRY=ghcr.io REGISTRY_PROJECT=your-org/clear

./deploy/podman/scripts/login-registry.sh
./deploy/podman/scripts/build-images.sh v1.0.0
./deploy/podman/scripts/push-images.sh  v1.0.0
```

`build-images.sh` reads the frontend's build arguments out of the env file,
because **Vite substitutes `import.meta.env` at build time**: `VITE_API_BASE_URL`
is baked into the bundle and cannot be corrected by an environment variable on the
VM. It also refuses `STORAGE_CONTAINER=contracts`, which would become an nginx
`location` shadowing the SPA's own `/contracts/<uuid>` route — every document
download would return the application's HTML with a 200.

`push-images.sh` reads each manifest back from the registry rather than trusting
the exit code: a push to a repository the account can write but not read fails
only on the VM, at pull time.

Tags must be versions. `latest` is refused: it cannot be rolled back to, and a VM
running it cannot say what it is running. Use `v1.0.0`, `2026-08-12-001`, or the
default `git-<short-sha>`.

Verify what landed:

```bash
podman manifest inspect ghcr.io/your-org/clear/clear-backend:v1.0.0 | head -20
```

### Step 20 — The document parser: `clear-extractor`

This deployment runs `ACTIVE_PARSER=pdfextract` against `clear-extractor`.

**It is not built, started or supervised by anything in this repository.** The
extractor is released from a separate repository; these scripts and units do not
reference it, and `start.sh` will not bring it up. Deploy and manage it
separately, and make sure it is running before the parser stage is exercised.

What this deployment requires of it:

| | |
|---|---|
| Container name | `clear-extractor` |
| Network | attached to `clear-net`, so `PDFEXTRACT_URL` resolves |
| Port inside `clear-net` | `8000` — what `PDFEXTRACT_URL=http://clear-extractor:8000` uses |
| Host port | `127.0.0.1:58001` for operator checks — **not** what the backend uses |
| Health | `GET /health` |
| Its own data path | `/datadrive/blob/CIP_Extraction`, unchanged by this work |

```bash
# From the host:
curl -fsS http://127.0.0.1:58001/health
# From inside the network, which is the one that matters:
podman exec clear-backend curl -fsS http://clear-extractor:8000/health
```

If the first succeeds and the second does not, the extractor is running but is not
attached to `clear-net`:

```bash
podman network connect clear-net clear-extractor
```

Alternatives, if the extractor is unavailable: `ACTIVE_PARSER=idoc` with
`IDOC_ENDPOINT` and `IDOC_API_KEY` (needs egress; `IDOC_VERIFY_TLS=true` is
enforced in production because contract text is uploaded to it), or
`ACTIVE_PARSER=pymupdf`, which is **degraded** — no layout JSON, so the
document-pipeline stage cannot run and clauses lose their page coordinates. Not
for production.

`PARSER_MODE=live`, never `fixture`: fixture replays a recorded response and in
production fails every document it has not already seen.

### Step 21 — Registry authentication on the VM

```bash
sudo -u clear -i
/opt/clear/deploy/podman/scripts/login-registry.sh
echo 'export REGISTRY_AUTH_FILE=$HOME/.config/containers/auth.json' >> ~/.bashrc
```

`XDG_RUNTIME_DIR` is per-login-session, so a token written by an interactive SSH
session is not necessarily visible to the systemd user manager. Pinning
`REGISTRY_AUTH_FILE` to a path in `$HOME` is what makes a hand-run `podman pull`
and a unit-run pull use the same credential.

### Step 22 — Pull the images

```bash
/opt/clear/deploy/podman/scripts/pull-images.sh
podman images
```

Pulling is separate from starting on purpose: it is the slow, failure-prone half,
and doing it first means the switchover is a restart measured in seconds. It also
means a registry outage at 03:00 cannot stop a rebooted VM from coming back —
everything is already in the local store.

### Step 23–24 — Volumes and units

The volumes are created by installing the units; the network already exists from
step 10 and is adopted rather than recreated.

```bash
/opt/clear/deploy/podman/scripts/install-quadlet.sh
```

It renders the templates, reloads the user manager, checks that **every** unit was
generated, and enables them at boot. A Quadlet file with a syntax error is
silently skipped rather than reported — the generated service simply does not
exist, and `systemctl start` then fails with "Unit not found", which reads like a
typo. That check is why the script verifies every unit in `CLEAR_UNITS`.

To create the volumes by hand instead:

```bash
podman volume create clear-storage
podman volume create clear-redis-data
podman volume ls
```

### Step 25–29 — Start everything

```bash
/opt/clear/deploy/podman/scripts/start.sh
```

Start order, which the units declare and the script walks explicitly so a failure
names the service that failed rather than a dependency chain that gave up:

```text
PostgreSQL (host, already running)
clear-extractor (already running, see §20)
      │
      ├─► clear-redis
      │
      └─► clear-migrate  (oneshot: systemd waits for exit 0)
                   │
                   ▼
             clear-backend ──► clear-worker-parser
                   │      └──► clear-worker-ai
                   │      └──► clear-queue
                   ▼
             clear-frontend
```

`clear-migrate.service` is `Type=oneshot`, which is what
`Requires=clear-migrate.service` on the backend and both workers hangs on: the API
**cannot** start against a half-built schema, and a failed migration stops the
deployment rather than producing a stack of 500s. It runs
`python -m app.cli migrate --seed` — Alembic to head, then the reference data and
the first administrator.

Before starting anything, `start.sh` refuses on: CRLF in the env file, quoted
values, empty required secrets, an empty `STORAGE_CONTAINER`, a
`STORAGE_LOCAL_ROOT` that disagrees with the path the units mount the volume at,
and a PostgreSQL that is not accepting connections.

That storage check is the one worth understanding. If `STORAGE_LOCAL_ROOT` names a
directory the volume is *not* mounted at, everything works — uploads succeed, the
viewer renders — right up until a container is recreated, at which point every
file written into the container's own layer is gone while `storage_path` in the
database still points at it.

### Step 30–33 — Verify

```bash
/opt/clear/deploy/podman/scripts/health-check.sh
```

One command covers unit states, container health, `/healthz` and `/readyz`, both
worker pools, the dispatcher, the frontend, the `/api/` proxy hop, Redis,
PostgreSQL *from inside the backend container*, the extractor at
`clear-extractor:8000`, whether every pipeline stage has a handler, and — for
local storage — that `/var/lib/cip/storage` is mounted and writable **by the
runtime user in all three containers that write to it**.

By hand:

```bash
podman ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
podman logs --tail 50 clear-backend
podman inspect clear-backend --format '{{.State.Health.Status}}'

podman exec clear-backend curl -fsS http://127.0.0.1:8000/healthz
podman exec clear-backend curl -fsS http://127.0.0.1:8000/readyz | head -c 600
podman exec clear-backend python -m app.cli stages
podman exec clear-backend python -m app.cli smoke        # reports every dependency
podman exec clear-backend python -m app.cli embeddings   # coverage and dimension
podman exec clear-redis redis-cli ping
curl -fsS http://127.0.0.1:9100/healthz                  # queue depths
curl -fsS http://127.0.0.1:8080/health                   # frontend
curl -fsS http://127.0.0.1:58001/health                  # extractor, from the host

# Local document storage: mounted, and writable by the container's own user.
podman volume inspect clear-storage
podman inspect clear-backend --format '{{range .Mounts}}{{.Source}} -> {{.Destination}}{{"\n"}}{{end}}'
podman exec clear-backend ls -ld /var/lib/cip/storage    # expect owner cip (uid 10001)
podman exec clear-backend sh -c 'touch /var/lib/cip/storage/.probe && rm /var/lib/cip/storage/.probe && echo writable'
```

Then the tests that separate a working deployment from a healthy-looking one:

1. **Log in** at `https://<host>/` with `SEED_ADMIN_EMAIL` and
   `SEED_ADMIN_PASSWORD`. Change the password when prompted.
2. **Upload several files at once.** Each gets its own `processing_jobs` row and
   they process concurrently, bounded by `WORKER_CONCURRENCY_<stage>`. Watch every
   contract walk validation → parser → docpipeline → extraction → embedding →
   indexing.
   ```bash
   podman logs -f clear-queue        # the dispatcher naming each stage
   podman logs -f clear-worker-ai
   ```
3. **Open a contract.** The PDF viewer fetches
   `GET /api/v1/contracts/{id}/content`, which streams from the storage volume
   through the `/api/` proxy. This is the end-to-end test that
   `STORAGE_LOCAL_ROOT`, `STORAGE_CONTAINER` and the volume mount all agree — and
   that a worker's write is visible to the API, which is the point of the three
   containers sharing one volume.
4. **Run an export and download it.**
5. **Confirm the sweeps run with no scheduler container.** Watch both worker pools:
   exactly one logs `alert_evaluator_swept` per tick, never both. That is the
   Postgres advisory lock working.
6. **Ask the Copilot something** and confirm the answer streams token by token
   rather than arriving whole — that is `proxy_buffering off` surviving both
   nginx hops.

### Step 34–35 — Reverse proxy and HTTPS

```bash
sudo apt-get install -y nginx certbot python3-certbot-nginx

sudo cp /opt/clear/deploy/podman/nginx/clear.conf /etc/nginx/sites-available/clear.conf
sudo sed -i 's/clear.example.com/<your-host>/g' /etc/nginx/sites-available/clear.conf
sudo ln -sf /etc/nginx/sites-available/clear.conf /etc/nginx/sites-enabled/clear.conf
sudo rm -f /etc/nginx/sites-enabled/default

sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d <your-host>          # rewrites the ssl_certificate lines
sudo systemctl enable --now certbot.timer    # renewal
```

On the RHEL family the file goes to `/etc/nginx/conf.d/clear.conf` and there is no
`sites-enabled` symlink. SELinux also blocks nginx from making outbound
connections until:

```bash
sudo setsebool -P httpd_can_network_connect 1
```

The config carries the four things that are not boilerplate: `client_max_body_size
120m` (larger than `MAX_UPLOAD_SIZE_MB`, so an oversized upload is refused by the
API with a JSON error the UI can show rather than by nginx with an HTML 413 it
cannot parse), `proxy_buffering off` (SSE and progressive PDF rendering),
`X-Forwarded-Proto` (without it every absolute URL the app generates comes out as
`http://`, which breaks the OIDC redirect), and 300-second timeouts.

Once the certificate is confirmed good, update `clear.env` and restart:

```bash
CORS_ORIGINS=https://<your-host>
OIDC_REDIRECT_URI=https://<your-host>/api/v1/auth/oidc/callback
OIDC_POST_LOGIN_REDIRECT=https://<your-host>/auth/callback
```

```bash
/opt/clear/deploy/podman/scripts/restart.sh
```

### Step 36 — Backups

```bash
sudo mkdir -p /var/backups/clear && sudo chown clear:clear /var/backups/clear
sudo -u clear crontab -e
```

```cron
0 2 * * * /opt/clear/deploy/podman/scripts/backup.sh >> /var/log/clear-backup.log 2>&1
```

```bash
sudo -u clear /opt/clear/deploy/podman/scripts/backup.sh    # run one now
```

### Step 37–38 — Reboot, and confirm automatic recovery

The single most valuable test in this document, because everything up to here also
passes on a stack that only exists because someone is logged in.

```bash
sudo reboot
# wait, then:
ssh <user>@<vm>
sudo -u clear -i
systemctl --user list-units 'clear-*'
/opt/clear/deploy/podman/scripts/health-check.sh
curl -fsS https://<your-host>/health
```

If nothing came back, linger is the first suspect:

```bash
loginctl show-user clear | grep Linger      # must be Linger=yes
```

### Step 39 — Rollback rehearsal

Rehearse it now, while nothing is wrong. §11.

---

## 6. Reaching the host's PostgreSQL from a container

The one piece of wiring with no single right answer, because `localhost` inside a
container is the container.

`install-postgres.sh` defaults `POSTGRES_HOST` to the podman bridge gateway and
falls back to the VM's primary private address. Verify it for real:

```bash
podman run --rm --network clear-net \
  ghcr.io/your-org/clear/clear-backend:v1.0.0 \
  python -c "import socket; socket.create_connection(('10.0.0.4', 5432), 5); print('reachable')"
```

If it fails, PostgreSQL names the address it rejected — which is the fastest route
to the right CIDR:

```bash
sudo journalctl -u postgresql@17-main | grep 'no pg_hba.conf entry'
sudo /opt/clear/deploy/podman/scripts/install-postgres.sh \
     --password '<same>' --allow-cidr <that address>/32
```

`host.containers.internal` is an alternative Podman resolves for containers, but
its behaviour differs across rootless network backends and Podman versions, so it
is not the default here. An explicit address always works.

---

## 7. Ports

| Port | Service | Exposure | Required |
|---:|---|---|---|
| 22 | SSH | **Public** — restrict by source in the security group | yes |
| 80 | nginx (host) | **Public** — ACME + redirect to 443 | yes |
| 443 | nginx (host) | **Public** — the only application ingress | yes |
| 8080 | `clear-frontend` | `127.0.0.1` only | yes |
| 8000 | `clear-backend` | `127.0.0.1` only | for operators |
| 9100 | `clear-queue` | `127.0.0.1` only | for operators |
| 58001 | `clear-extractor` | `127.0.0.1` only | for operators |
| 8000 | `clear-extractor` **inside** `clear-net` | **podman network only** | internal — this is the port `PDFEXTRACT_URL` uses |
| 8001 | worker pools | **podman network only**, not published | internal |
| 6379 | `clear-redis` | **podman network only**, not published | internal |
| 5432 | PostgreSQL | host, `localhost` + one bridge address | internal |

No object-storage ports: there is no object store.

**Must never be public:** 5432, 6379, 8000, 8001, 9100, 58001. An exposed Redis
with no password is a remote code execution primitive, not a database.

Reach a loopback port from your laptop with a tunnel, not a firewall rule:

```bash
ssh -L 9100:127.0.0.1:9100 -L 8000:127.0.0.1:8000 <user>@<vm>
```

---

## 8. Volumes

| Volume | Mounted at | Holds | Persistent | If the container is deleted |
|---|---|---|---|---|
| `clear-storage` | `/var/lib/cip/storage` in backend + **both workers** | every uploaded contract and generated export, plus benchmark results and parser fixtures | **critical** | survives; `podman volume rm` does not |
| `clear-redis-data` | `/data` in `clear-redis` | BullMQ queue state | minutes | survives |
| PostgreSQL | `/var/lib/postgresql/17/main` **on the VM** | everything else | **critical** | not a container; unaffected |
| container temp | image writable layer | LibreOffice conversions, pdfextract scratch | no | recreated |

`clear-storage` is mounted read-write by three containers on purpose: a worker
writes the converted PDF that the API later streams to the viewer. All three must
mount it at the same path, and `STORAGE_LOCAL_ROOT` must equal that path.

`clear-storage` and PostgreSQL are **one backup**. `documents.storage_path` in the
database is a path into that volume, so a database restored newer than the volume
references files that do not exist — the application looks perfectly healthy while
every document download 404s. Never restore one without the other.

`clear-redis-data` is deliberately **not** backed up: restoring a day-old queue
would replay work already done. If it is ever lost, re-enqueue with
`podman exec clear-backend python -m app.cli reprocess ...` for anything left in a
non-terminal state.

```bash
podman volume ls
podman volume inspect clear-storage
du -sh "$(podman volume inspect clear-storage --format '{{.Mountpoint}}')"
podman system df -v          # what is actually using the disk
```

---

## 9. Startup order and dependencies

| From | To | Why |
|---|---|---|
| `clear-backend` | PostgreSQL | everything |
| `clear-backend` | `clear-storage` volume | document bytes; gates `/readyz` via the adapter's own write probe. A mount, not a service, so there is nothing to start or order against — but a missing mount fails readiness |
| `clear-backend` | `clear-redis` | cache, rate limits — **fails open**, so not gated |
| `clear-queue` | `clear-redis` | the broker itself; hard requirement |
| `clear-queue` | `clear-backend` / workers | POSTs each stage over internal HTTP |
| workers | PostgreSQL, `clear-storage` volume | run the stages; the parser worker writes the converted PDF the API later streams |
| workers | `clear-extractor` | the parser stage POSTs to `PDFEXTRACT_URL` |
| `clear-frontend` | `clear-backend` | `/api` proxy; resolved per request, so a backend restart does not need a frontend restart |
| everything | `clear-migrate` | schema must exist first |

Redis is **not** checked by `/readyz`, deliberately: every cache and rate-limit
call fails open, so probing for it could only report a problem that is not one —
and reporting it would hold the container out of rotation indefinitely.

---

## 10. Updating

```bash
# build machine
./deploy/podman/scripts/build-images.sh v1.0.1
./deploy/podman/scripts/push-images.sh  v1.0.1

# VM
sudo -u clear -i
/opt/clear/deploy/podman/scripts/update.sh v1.0.1
```

`update.sh` does, in this order: pull (old version still serving) → record the
current tag as `PREVIOUS_IMAGE_TAG` → re-render the units → run migrations
(oneshot; a failure stops here with nothing restarted) → restart the application
containers → **health gate**. If the gate fails it rolls back automatically.

```text
                   ┌── healthy ──► keep v1.0.1
pull → migrate → restart ─┤
                   └── unhealthy ─► automatic rollback to v1.0.0
```

Previous images are kept locally on purpose — a rollback that has to pull from a
registry is a rollback that fails during the outage it exists for. Reclaim the
disk only once the new version has proven itself:

```bash
podman image prune --all --filter 'until=168h'
```

---

## 11. Rollback

```bash
/opt/clear/deploy/podman/scripts/rollback.sh            # to PREVIOUS_IMAGE_TAG
/opt/clear/deploy/podman/scripts/rollback.sh v1.0.0     # to a specific tag
```

Manually, the same thing:

```bash
sed -i 's/^IMAGE_TAG=.*/IMAGE_TAG=v1.0.0/' /etc/clear/images.env
/opt/clear/deploy/podman/scripts/install-quadlet.sh
systemctl --user restart clear-backend clear-worker-parser clear-worker-ai clear-queue clear-frontend
```

**What rolls back:** the images, the unit files that name them, the recorded tag.

**What does not:**

* **The database schema.** Additive migrations — a new table, a new nullable
  column — are harmless with an older image. A migration that **dropped or
  retyped** something is not, and the old image fails on the missing column. Run
  the downgrade with the **new** image *before* rolling back, because the
  downgrade script only exists in the release that introduced it:
  ```bash
  podman run --rm --network clear-net --env-file /etc/clear/clear.env \
    ghcr.io/your-org/clear/clear-backend:v1.0.1 \
    alembic downgrade -1
  ```
  `alembic` directly rather than `app.cli downgrade`, because Typer parses the
  leading `-1` as an option. Through the CLI it needs an explicit separator:
  `python -m app.cli downgrade --yes -- -1`. Either way `migrations/env.py` takes
  the connection string from the same settings the application uses, so no extra
  configuration is needed. Check where you are first with
  `podman exec clear-backend python -m app.cli current`.
* **Uploaded documents.** Nothing here touches the `clear-storage` volume.
* **`clear.env`.** Configuration is not versioned with the image. If the release
  needed a new setting, remove it by hand.

**When a database restore is required instead:** any release that destroyed data —
a dropped column, an irreversible data-rewriting migration. Then the order is
stop → restore the dump → restore the `clear-storage` volume *from the same
night* → roll the image back.

---

## 12. Backup and recovery

```bash
/opt/clear/deploy/podman/scripts/backup.sh                    # /var/backups/clear
/opt/clear/deploy/podman/scripts/backup.sh /mnt/backups
```

Three artefacts per run:

| Artefact | Command underneath | Retention |
|---|---|---|
| `postgres-cip.dump` | `pg_dump -Fc --compress=6` | 14 days locally, longer offsite |
| `clear-storage.tar.gz` | `podman volume export clear-storage` | same |
| `config/` | `clear.env`, `images.env`, rendered units, nginx config | same |

The second one is every contract in the system, so the script **fails** rather
than warns if the volume is absent — a "successful" backup with no documents in it
is only discovered during a restore, which is the worst possible moment.

`-Fc`, the custom format, not plain SQL: compressed, and restorable
table-by-table with `pg_restore`. A plain dump of a database with millions of
vector rows is enormous and can only be restored whole.

The script **verifies** the dump by parsing it with `pg_restore --list` and fails
the backup if it does not parse. A backup nobody has read back is a hypothesis; a
size check would not catch a truncated archive.

**A copy that only exists on this VM is not a backup.** Ship it off the host:

```bash
rsync -a --delete /var/backups/clear/ <offsite>:/backups/clear/
```

Restore — both artefacts, together:

```bash
/opt/clear/deploy/podman/scripts/stop.sh

pg_restore --host 172.22.20.132 --username cip --dbname cip \
           --clean --if-exists /var/backups/clear/<stamp>/postgres-cip.dump

gunzip -c /var/backups/clear/<stamp>/clear-storage.tar.gz > /tmp/clear-storage.tar
# DESTRUCTIVE: this discards whatever documents are on the volume now. Take a
# fresh export of it first if the current contents matter at all.
podman volume rm clear-storage && podman volume create clear-storage
podman volume import clear-storage /tmp/clear-storage.tar

/opt/clear/deploy/podman/scripts/start.sh
```

**PostgreSQL upgrade note.** A server refuses to start on a data directory written
by a different major version, and the error reads like corruption rather than a
version mismatch. A major upgrade is `pg_upgrade` or dump-and-reload, with pgvector
installed for the new major **before** the upgrade — a missing extension there
fails the upgrade partway.

```bash
df -h /var/lib/postgresql        # watch the data disk
sudo -u postgres psql -d clear -c \
  "SELECT pg_size_pretty(pg_database_size('clear'));"
```

---

## 13. Troubleshooting

```bash
podman ps -a
podman logs --tail 100 <container>
podman inspect <container>
systemctl --user status clear-backend.service
journalctl --user -u clear-backend.service -n 100 --no-pager
```

### A container does not start

```bash
systemctl --user status clear-backend.service -l
journalctl --user -u clear-backend.service -n 100 --no-pager
podman logs clear-backend
```

`Unit clear-backend.service not found` almost never means a typo — it means
Quadlet did not generate the unit, which it does silently on a parse error:

```bash
systemctl --user daemon-reload
journalctl --user -t quadlet-generator -n 50 --no-pager
```

`Failed to load environment files` is a permission problem on
`/etc/clear/clear.env`, and the message does not say so. It must be readable by
the `clear` user.

### An image cannot be pulled

```bash
podman login <registry>
podman manifest inspect <registry>/<project>/clear-backend:<tag>
podman images
echo $REGISTRY_AUTH_FILE
```

If it works by hand and fails from a unit, the credential is in a per-session
`XDG_RUNTIME_DIR` the systemd user manager cannot see — step 21.

### The backend cannot reach PostgreSQL

In likelihood order:

```bash
sed -n 's/^DATABASE_URL=//p' /etc/clear/clear.env      # quoted? \r at the end?
systemctl status postgresql@17-main
sudo ss -lntp | grep 5432                              # listen_addresses correct?
sudo journalctl -u postgresql@17-main | grep 'no pg_hba.conf entry'
podman exec clear-backend python -c \
  "import asyncio; from app.db.session import database_healthy; print(asyncio.run(database_healthy()))"
```

`no pg_hba.conf entry for host X` names the exact address to allow — re-run
`install-postgres.sh --allow-cidr X/32`.

### The backend cannot reach Redis

```bash
podman ps --filter name=clear-redis
podman exec clear-redis redis-cli ping
podman network inspect clear-net --format '{{range .Containers}}{{.Name}} {{end}}'
sed -n 's/^REDIS_URL=//p' /etc/clear/clear.env         # expect redis://clear-redis:6379/0
podman logs --tail 50 clear-redis
```

Redis is not gated by `/readyz`, so the backend will look healthy while the queue
does nothing.

### The worker is not processing jobs

Uploads are accepted, status never advances. Work down this list:

```bash
podman logs --tail 100 clear-queue            # is the dispatcher pulling anything?
curl -fsS http://127.0.0.1:9100/healthz       # queue depths and dead-letter size
podman logs --tail 100 clear-worker-ai
podman exec clear-backend python -m app.cli stages   # every stage has a handler?
```

Then the two configuration traps that produce exactly this symptom:

1. **`QUEUE_DRIVER` and the worker command disagree.** `bullmq` needs
   `uvicorn app.worker_app:app`, which *serves* the dispatcher's endpoints and
   polls nothing. `postgres` needs `python -m app.cli worker`, which *claims* rows
   from `stage_queue`. Run the wrong one and every container is healthy while
   every upload stays `pending` for ever.
2. **`INTERNAL_API_TOKEN` differs** between the backend and the dispatcher. They
   read the same env file here, so this only happens after a partial edit — the
   dispatcher logs 401s.

```sql
-- what the pipeline thinks is happening
SELECT status, count(*) FROM processing_jobs GROUP BY status;
```

### The frontend cannot reach the backend

```bash
podman exec clear-frontend wget -qO- http://127.0.0.1:8080/api/v1/auth/methods
podman exec clear-frontend cat /etc/nginx/conf.d/default.conf | head -40
podman logs --tail 50 clear-frontend
curl -fsS https://<host>/api/v1/auth/methods
```

A literal `${NGINX_LOCAL_RESOLVERS}` in the generated config means
`NGINX_ENTRYPOINT_LOCAL_RESOLVERS` was lost — nginx exits 1 with `host not found
in resolver` and the container restart-loops, serving nothing.

**Login fails with no request reaching the backend** is almost always a frontend
image built with the wrong `VITE_API_BASE_URL`. It is baked into the bundle;
rebuild the image, do not edit anything on the VM.

### Uploaded files disappear when a container is replaced

The signature failure of local storage. Documents upload fine, the viewer renders,
and then a `restart.sh` or an `update.sh` loses everything written since the last
one — because the bytes were going into the container's writable layer rather than
onto the volume, while `storage_path` in the database still points at them.

```bash
podman volume ls                                       # is clear-storage there?
podman inspect clear-backend --format '{{range .Mounts}}{{.Source}} -> {{.Destination}}{{"\n"}}{{end}}'
sed -n 's/^STORAGE_LOCAL_ROOT=//p' /etc/clear/clear.env  # must equal the destination above
sed -n 's/^STORAGE_PROVIDER=//p' /etc/clear/clear.env    # expect: local
podman exec clear-backend ls -la /var/lib/cip/storage
```

`start.sh` refuses to start on this mismatch, so it should not reach production —
but a hand-run `podman run` without the volume flag reproduces it exactly.

Check both worker pools too, not just the API: a mount present in the backend and
missing in `clear-worker-parser` means uploads survive and converted PDFs do not.

### Every document download 404s

1. **`STORAGE_CONTAINER` was changed after documents were uploaded.** It is part of
   every stored path, so existing rows point into the old subdirectory. Look under
   `/var/lib/cip/storage/` for a stale directory name — the files are usually still
   there, under the previous value.
2. **`STORAGE_LOCAL_ROOT` disagrees with the mount**, so the API is reading an
   empty directory. Same checks as above.
3. **The file was written by a worker whose mount is missing**, so it never reached
   the volume at all.

```bash
# What the database thinks, versus what is on disk.
podman exec clear-backend python -m app.cli shell
podman exec clear-backend find /var/lib/cip/storage -maxdepth 2 -type d
```

### The backend restart-loops at boot

Almost always one of two things, and both are in the logs:

```bash
podman logs clear-backend | head -40
```

* `Refusing to start in production with insecure configuration` — the guard listing
  exactly which settings are wrong.
* An embedding probe failure. `EMBEDDING_VERIFY_ON_STARTUP=true` refuses to boot
  when the endpoint is unreachable or its dimension disagrees with the column.
  **Fix the endpoint; do not turn the check off.** Turning it off ships silently
  broken retrieval.

### Alerts never appear

Every worker has `WORKER_MAINTENANCE=false` and no standalone scheduler is
running. There is no scheduler container by design — the sweeps run inside every
worker behind a Postgres advisory lock. Confirm exactly one worker logs
`alert_evaluator_swept` per tick:

```bash
podman logs clear-worker-ai | grep alert_evaluator_swept
podman logs clear-worker-parser | grep alert_evaluator_swept
```

### Disk filling up

```bash
podman system df -v
du -sh /var/lib/postgresql/17/main
du -sh "$(podman volume inspect clear-storage --format '{{.Mountpoint}}')"
journalctl --user --disk-usage
podman image prune --all --filter 'until=168h'
sudo journalctl --vacuum-time=14d
```

With local storage the document volume grows without bound — it holds every
contract ever uploaded plus every export. Watch it alongside the database, and
note that export retention is swept by the workers while contract files are kept
for as long as the contract row exists.

Container logs go to journald (`LogDriver=journald`), so cap it in
`/etc/systemd/journald.conf`:

```ini
SystemMaxUse=2G
MaxRetentionSec=2week
```

---

## 14. Security

Enforced by this deployment:

- [x] No secrets in Dockerfiles, and `.dockerignore` excludes `.env`/`.env.*` from
      every build context.
- [x] No secrets in git — `.gitignore` covers `.env` and `.env.*`; only the
      annotated `.env.production.example` is tracked.
- [x] `/etc/clear/*.env` is `0600`, owned by the deployment user.
- [x] PostgreSQL listens on localhost plus one bridge address, `scram-sha-256`,
      one rule per allowed CIDR, scoped to one database and one role. A
      `0.0.0.0/0` rule makes the install script abort.
- [x] Redis is not published at all.
- [x] Document bytes are never served directly. Every read goes through
      `GET /api/v1/contracts/{id}/content`, which applies the caller's own
      authorisation — there is no public path to a file and no presigned URL that
      outlives a session.
- [x] Containers run rootless, and the processes inside them are non-root
      (`cip` uid 10001, `node`, `nginx`). `NoNewPrivileges=true` on every unit.
- [x] Secrets are injected at runtime via `--env-file`, never baked into an image.
- [x] `DEBUG=false` and `APP_ENV=production` — enforced by a guard that refuses to
      boot otherwise. Swagger is disabled.
- [x] No development servers: nginx serves a built bundle, gunicorn serves the API.
      The Vite dev server and the `dev` image targets are not used.
- [x] Registry credentials in `$HOME/.config/containers/auth.json`, mode 600.
- [x] Only 22, 80, 443 are public. HTTPS with HSTS; 80 redirects.
- [x] No object-storage credentials exist at all — local storage needs none, which
      removes a whole class of key to leak, rotate or misconfigure.

Left to you, because they are policy rather than configuration:

- [ ] Restrict SSH by source address in the cloud security group, and disable
      password authentication (`PasswordAuthentication no`).
- [ ] Rotate `JWT_SECRET` and `INTERNAL_API_TOKEN` on a schedule. Rotating the JWT
      secret invalidates every issued token — every user is logged out.
- [ ] Decide who may read `/var/backups/clear`. Those dumps are every contract in
      the system, in plain text — and so is the `clear-storage` export beside them.
- [ ] Decide who may read the `clear-storage` volume's mountpoint on the host. Any
      account that can reach it can read every contract, bypassing the API's
      authorisation entirely. Under rootless podman it sits under the deployment
      user's `~/.local/share/containers`, which is the right default.

---

## 15. Open items — `REQUIRES CONFIRMATION`

Nothing below can be derived from this repository. Each has a placeholder that
will fail visibly rather than silently.

| # | Item | Where | If it is wrong |
|---|---|---|---|
| 1 | Container registry host and project | `/etc/clear/images.env` | pull fails immediately |
| 2 | Public hostname and DNS | `nginx/clear.conf`, `CORS_ORIGINS`, `OIDC_*` | certificate fails; SSO callback rejected |
| 3 | LLM vendor and key | `LLM_PROVIDER` + credentials | extraction fails; `mock` is refused in production |
| 4 | Embedding vendor, model, dimension | `EMBEDDING_*` | **backend refuses to boot** — deliberately |
| 5 | `POSTGRES_HOST` as seen from a container | `clear.env` | migration fails; §6 |
| 6 | Whether Entra SSO is used | `OIDC_*` **and** the frontend build arg | SSO button absent, or callback rejected |
| 7 | Offsite backup destination | crontab / `rsync` | backups exist only on the VM being backed up |
| 8 | **How `clear-extractor` is deployed and managed** | separate repository | see §20 — this deployment does not build, start or supervise it |

---

## 16. Final checklist

```text
[ ] VM provisioned                        [ ] Podman network created
[ ] OS verified                           [ ] Persistent volumes created
[ ] Podman installed                      [ ] Production environment configured
[ ] Podman verified (>= 4.4, Quadlet)     [ ] Backend started
[ ] Deployment user created               [ ] Worker pools started
[ ] Linger enabled                        [ ] Queue dispatcher started
[ ] PostgreSQL installed                  [ ] Frontend started
[ ] PostgreSQL running and enabled        [ ] All containers healthy
[ ] pgvector installed                    [ ] Backend -> PostgreSQL verified
[ ] pgvector extension enabled            [ ] Backend -> Redis verified
[ ] Vector round-trip through HNSW        [ ] Backend -> extractor verified
[ ] Database created                      [ ] Worker -> Redis verified
[ ] Database user created                 [ ] Worker -> PostgreSQL verified
[ ] pg_hba.conf scoped, no 0.0.0.0/0      [ ] Frontend -> Backend verified
[ ] Database migrations completed         [ ] File upload tested
[ ] Seed admin created                    [ ] Document processing tested end to end
[ ] Redis configured                      [ ] Document download via /content tested
[ ] clear-extractor running on clear-net  [ ] Export generated and downloaded
[ ] Registry authentication configured    [ ] Copilot streams token by token
[ ] Images built                          [ ] Alert sweeps observed (exactly one worker)
[ ] Images tested                         [ ] Firewall configured (22/80/443 only)
[ ] Images tagged with a version          [ ] Internal ports confirmed unreachable
[ ] Images pushed and read back           [ ] Reverse proxy configured
[ ] Images pulled on the VM               [ ] HTTPS configured, HSTS on
[ ] Quadlet/systemd units installed       [ ] PostgreSQL backup configured
[ ] Units enabled at boot                 [ ] clear-storage volume backup configured
[ ] VM reboot tested                      [ ] Backup restore rehearsed
[ ] Containers recovered automatically    [ ] Rollback procedure rehearsed

--- local document storage --------------------------------------------------
[ ] STORAGE_PROVIDER=local
[ ] clear-storage volume created
[ ] Mounted at /var/lib/cip/storage in clear-backend
[ ] Mounted at /var/lib/cip/storage in clear-worker-parser
[ ] Mounted at /var/lib/cip/storage in clear-worker-ai
[ ] STORAGE_LOCAL_ROOT matches that mount path
[ ] STORAGE_CONTAINER set, and recorded as never-to-change
[ ] Writable by the runtime user (uid 10001) in all three containers
[ ] A worker's write is visible to the API (upload, then view the document)
[ ] Files survive a container recreate (restart.sh, then re-open the document)
```

---

## 17. Command reference

```bash
# lifecycle
/opt/clear/deploy/podman/scripts/start.sh
/opt/clear/deploy/podman/scripts/stop.sh
/opt/clear/deploy/podman/scripts/restart.sh [service]
/opt/clear/deploy/podman/scripts/health-check.sh [--wait]

# releases
/opt/clear/deploy/podman/scripts/pull-images.sh [tag]
/opt/clear/deploy/podman/scripts/update.sh <tag>
/opt/clear/deploy/podman/scripts/rollback.sh [tag]

# what is deployed
grep -h '^Image=' ~/.config/containers/systemd/*.container
grep IMAGE_TAG /etc/clear/images.env

# observation
podman ps --format 'table {{.Names}}\t{{.Status}}'
podman logs -f clear-backend
journalctl --user -u clear-worker-ai.service -f
systemctl --user list-units 'clear-*'

# application
podman exec clear-backend python -m app.cli stages
podman exec clear-backend python -m app.cli smoke
podman exec clear-backend python -m app.cli embeddings
podman exec clear-backend python -m app.cli current        # alembic revision
podman exec -it clear-backend python -m app.cli shell

# scaling the AI pool
cp ~/.config/containers/systemd/clear-worker-ai.container \
   ~/.config/containers/systemd/clear-worker-ai-2.container
sed -i 's/clear-worker-ai/clear-worker-ai-2/' ~/.config/containers/systemd/clear-worker-ai-2.container
systemctl --user daemon-reload && systemctl --user enable --now clear-worker-ai-2.service
```

---

## 18. Related documents

| Document | Covers |
|---|---|
| [DEPLOYMENT_VM.md](DEPLOYMENT_VM.md) | the earlier Docker Compose deployment; still the reference for the pipeline's shape |
| [PIPELINE_FLOW.md](PIPELINE_FLOW.md) | what each stage does |
| [BACKEND_GUIDE.md](BACKEND_GUIDE.md) | the API and its internals |
| [DATABASE_SCHEMA.md](DATABASE_SCHEMA.md) | tables, indexes, the vector columns |
| [alerting.md](alerting.md) | the sweeps and their configuration |
| [../deploy/podman/README.md](../deploy/podman/README.md) | a short index of the files above |
