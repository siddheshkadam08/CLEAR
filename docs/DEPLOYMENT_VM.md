# Deploying CLEAR to a VM

> **Scope.** This document describes the **Docker Compose** deployment, which uses
> MinIO for object storage. The production Podman deployment is a different shape —
> no MinIO, documents on a local filesystem volume, PostgreSQL installed on the host
> — and is documented in [PODMAN_DEPLOYMENT.md](PODMAN_DEPLOYMENT.md). Use that one
> for a production VM.
>
> Two files named below, `docker-compose.vm.yml` and `docker-compose.override.yml`,
> have since been removed from the repository.

The stack is BullMQ over Redis for the queue, MinIO for object storage, and one
container per service. There is **no `scheduler` container**: its periodic sweeps run
inside the workers behind a Postgres advisory lock — see §3.

| # | Container | Role | Published |
|---|-----------|------|-----------|
| 1 | `cip-postgres` | Database + pgvector | `127.0.0.1:5432` |
| 2 | `cip-redis` | BullMQ broker, cache, rate-limit counters | `127.0.0.1:6379` |
| 3 | `cip-minio` (+ `minio-init`) | Object storage; init creates the three buckets | `127.0.0.1:9000` |
| 4 | `cip-queue` | Node BullMQ dispatcher — `Queue` + `Worker` in one process | `127.0.0.1:9100` |
| 5 | `cip-backend` | The API | `127.0.0.1:8000` |
| 6 | `cip-worker-parser` | Stages 1–5, CPU/IO-bound | — |
| 7 | `cip-worker-ai` | Stages 6–8, latency-bound on the model provider | — |
| 8 | `cip-frontend` | nginx: SPA, `/api` proxy, and the object-storage proxy | **`0.0.0.0:80`** |
| — | `cip-migrate` | One-shot `cli migrate --seed`, then exits 0 | — |

Optional: `otel-collector`, `prometheus`, `grafana`, `jaeger`. Leave them out and set
`OTEL_ENABLED=false`.

**One public port.** `frontend/nginx.conf` proxies the API *and* the storage bucket on
the same origin, so the refresh cookie stays first-party and presigned download URLs are
fetchable without a second firewall hole.

---

## 1. How a multi-file upload flows

Worth understanding before deploying, because it is where most questions land.

1. The UI sends **every file in one request**. `MAX_FILES_PER_UPLOAD` caps it at 100.
2. `UploadService` expands any ZIP so each member takes the same route as a directly
   uploaded file, then per file: validate → store → create contract → **create one
   `processing_jobs` row**.
3. Every job is enqueued after the rows are flushed, so a worker cannot claim a job whose
   contract is not yet visible.
4. The API returns `202` with a per-file outcome. No parsing happens in the request — a
   batch may partially succeed, and `files[].status` reports each one.
5. The dispatcher pulls each job and POSTs it to a worker pool, which runs the stage and
   returns what happens next. Each stage commits its checkpoint before the next is queued.

Files therefore process **concurrently**, bounded by `WORKER_CONCURRENCY_<stage>`
(validation 10, parser 20, docpipeline 4, extraction 4, embedding 15, indexing 5). That
is deliberate: extraction is mostly time spent waiting on the model provider, so
parallelism is where the throughput comes from.

---

## 2. Why each service is there

**`queue`** is the BullMQ dispatcher: `Queue` (producer) and `Worker` (consumer) in one
Node process. It takes a message off Redis and POSTs it to
`/internal/stages/{stage}/run`; every decision — which stage runs, whether a failure is
retryable, how long to back off — is made in Python and returned in the response body.
BullMQ v5 needs **no `QueueScheduler`**: delayed-job promotion and stalled-job recovery
moved into `Worker` at v4.

**`worker-parser` / `worker-ai`** run the stages. They are not strictly required — the
API mounts the same internal endpoints, so `INTERNAL_API_BASE_URL` could point at
`backend` — but a 10-minute extraction running in the API's process pool makes logins
queue behind contract parsing. The two pools are split because they scale differently.

**`redis`** is the broker. The cache and rate limiter also use it, and both fail open.

---

## 3. There is no scheduler container

Four sweeps have to happen on a timer, and none of them is anything BullMQ can do:

| Sweep | What its absence costs |
|---|---|
| Alert evaluation | Expiry, renewal-notice and obligation deadlines are noticed by nothing else. The Alerts screen stays empty and contracts silently lapse. |
| Stalled-job reclamation | A crashed worker leaves a job `processing` for ever. The dashboard reports the count; only the sweep resolves it. |
| Stalled-export recovery | An export dies with its process; the row survives as `running`. |
| Expired-export purge | Export files outlive their retention window as second copies of contract data. |

They run inside **every worker**, guarded by `pg_try_advisory_lock` on a dedicated
connection, so exactly one worker performs them per tick. That removes a container and a
single point of failure: a lost scheduler took alert evaluation with it silently.

`WORKER_MAINTENANCE=false` plus `python -m app.cli scheduler` splits them back out.

---

## 4. VM specification

| | Recommended | Minimum |
|---|---|---|
| vCPU | 4 | 2 |
| RAM | 16 GB | 8 GB + 4 GB swap |
| Disk | 100 GB SSD | 60 GB |
| OS | Ubuntu 22.04 / 24.04 LTS | any Docker-capable Linux |

The backend image is large (~2.5 GB: LibreOffice, Tesseract, Poppler) and three
containers run it. Docker Engine + Compose v2 is assumed; Podman works (the compose file
carries `:z` labels and every published port is above 1024) but cannot bind port 80
rootless without extra setup.

`docker-compose.vm.yml` is a **separate, three-container file** for a constrained host
that already runs Postgres and the extractor natively. It has ~1 GB free and cannot fit
Redis plus a Node dispatcher, so it sets `QUEUE_DRIVER=postgres` and runs
`python -m app.cli worker`, which claims from the `stage_queue` table directly. Do not
copy its settings into the main stack.

---

## 5. The one external dependency: the PDF parser

`.env` ships `PDFEXTRACT_PATH` pointing at a Windows checkout, which will not exist on a
Linux VM. Pick one:

| Option | Configuration | Notes |
|--------|---------------|-------|
| **Extractor as another container** | `ACTIVE_PARSER=pdfextract`, `PDFEXTRACT_URL=http://extractor:8005/extract?format=adi` | Self-contained. Needs the `pdf_text_extractor` repo and a Dockerfile building its venv **in-image** — a Windows-built `.venv` will not run. |
| **An extractor already running elsewhere** | Same two, host address | No build work; couples the demo to another box. |
| **Hosted iDoc** | `ACTIVE_PARSER=idoc`, `IDOC_ENDPOINT`, `IDOC_API_KEY` | Needs egress. |
| **`pymupdf`** | `ACTIVE_PARSER=pymupdf` | **Degraded**: no layout JSON, so the document pipeline stage cannot run and clauses lose their page coordinates. |

`PDFEXTRACT_URL` and the `IDOC_*` keys must be in the compose `x-backend-env` block to
reach the container at all — that block is an allow-list, not a passthrough.

Also needs egress: the LLM endpoint and the embedding endpoint.
`EMBEDDING_VERIFY_ON_STARTUP` is on, so the backend **refuses to boot** if the embedding
endpoint is unreachable or its dimension disagrees with the column — deliberately,
because a wrong dimension degrades answers silently for as long as it runs.

---

## 6. Deploying

```bash
sudo apt-get update && sudo apt-get install -y ca-certificates curl git
# Docker Engine + compose plugin, then:
sudo usermod -aG docker "$USER" && newgrp docker
echo '{"log-driver":"json-file","log-opts":{"max-size":"10m","max-file":"3"}}' \
  | sudo tee /etc/docker/daemon.json && sudo systemctl restart docker
sudo ufw allow 22/tcp && sudo ufw allow 80/tcp && sudo ufw enable
```

Open only 22 and 80 in the cloud security group too. Everything else is on `127.0.0.1`;
reach it over an SSH tunnel.

```bash
./scripts/deploy-remote.sh <user>@<vm> -i <key>   # .env is excluded on purpose
```

`scripts/deploy.sh` runs on the host: it generates secrets on first run, never overwrites
an existing `.env`, moves the dev-only `docker-compose.override.yml` aside, builds, and
waits for the API to report healthy.

`FRONTEND_PORT=80` and `FRONTEND_TARGET_PORT=8080` — the runtime image serves nginx on
8080 while the dev override runs Vite on 5173. Leaving the target at 5173 publishes a
host port against a container port nothing listens on.

---

## 7. Verification

```bash
docker compose ps          # 8 up, migrate + minio-init Exited (0), no scheduler
curl -fsS http://127.0.0.1:8000/healthz
curl -fsS http://127.0.0.1:8000/readyz
docker compose exec backend python -m app.cli stages
docker compose exec backend python -m app.cli smoke
make queue-status          # BullMQ depths and dead-letter size
```

`curl -fsS`, not bare `curl`: a plain `curl` exits 0 on a 404. Liveness is `/healthz` at
the root, **not** under `/api/v1`.

Then the tests that distinguish a working deployment from a healthy-looking one:

1. **The pipeline.** Upload several files. Each gets its own job; the dispatcher logs it,
   and each contract walks validation → parser → docpipeline → extraction → embedding →
   indexing.
2. **The sweeps run without a scheduler container.** Watch both worker pools: exactly one
   logs `alert_evaluator_swept` per tick, never both. That is the advisory lock working.
3. **Alerts fire.** With a contract expiring inside `ALERT_EXPIRY_WINDOW_DAYS`, confirm
   the Alerts screen populates without anyone running `cli evaluate-alerts` by hand.
4. **Downloads.** Open a contract (presigned URL through the nginx bucket proxy) and run
   an export and download it.
5. **Reboot.** `docker compose down && up -d`, or restart the VM, and confirm everything
   returns — every long-running service declares `restart: unless-stopped`.

---

## 8. Operations

```bash
docker compose logs -f backend worker-parser worker-ai queue
docker compose up -d --scale worker-ai=4
docker compose exec -T postgres pg_dump -U cip -Fc cip > "cip-$(date +%F).dump"
docker run --rm -v cip_miniodata:/data -v "$PWD:/backup" alpine tar czf /backup/minio-$(date +%F).tgz /data
```

| Symptom | Cause |
|---|---|
| Upload accepted, status never advances | `queue` cannot reach Redis or the workers; check `make queue-status` |
| Backend restart-loops at boot | Embedding endpoint unreachable, or dimension mismatch |
| Login fails, no request in the backend log | `VITE_API_BASE_URL` baked as `http://localhost:8000/api/v1` — rebuild the frontend image |
| Every document download 404s | `S3_PUBLIC_ENDPOINT_URL` not browser-reachable, or the bucket name shadows an SPA route |
| Alerts never appear | Every worker has `WORKER_MAINTENANCE=false` and no standalone scheduler is running |
| Parser fails with a missing binary | `PDFEXTRACT_URL` not in the compose allow-list, so the adapter fell back to the subprocess path |
