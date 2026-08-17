# Contract Intelligence Platform (CIP)

Enterprise AI contract intelligence: ingest PDF/DOCX contracts, extract structured
legal knowledge with AI, and serve **explainable, evidence-grounded** answers,
search, analytics and reports.

Every extracted fact carries its provenance — document, page, section, bounding box,
confidence, and the exact parser/prompt/model/embedding versions used to produce it.

---

## Table of contents

- [Architecture at a glance](#architecture-at-a-glance)
- [Quick start](#quick-start)
- [Repository layout](#repository-layout)
- [The processing pipeline](#the-processing-pipeline)
- [The retrieval and answer path](#the-retrieval-and-answer-path)
- [Core design rules](#core-design-rules)
- [Configuration](#configuration)
- [Development](#development)
- [Observability](#observability)
- [Deployment](#deployment)
- [Further reading](#further-reading)

---

## Architecture at a glance

```
React SPA (Vite + TS)
        │  HTTPS / JWT
        ▼
FastAPI API Gateway ── AuthN/AuthZ · RBAC · rate limit · OpenTelemetry
        │
        ├── Project / User service        ├── Search / Retrieval service
        ├── Contract / Upload service     ├── RAG / Copilot service
        └── Admin / Clause Master service
        │
        ├──────────────► PostgreSQL 16 + pgvector (HNSW)
        ├──────────────► Redis (cache · session · rate limit · queue)
        └──────────────► Object storage (Azure Blob | S3 | MinIO | local)

Processing Orchestrator
   Workflow Engine  (decision plane — WHAT runs)
   Execution Engine (execution plane — HOW it runs)
        │  enqueue via BullMQ
        ▼
Worker pools: Validation · Parser · Enrichment · Classification
              Chunking · AI Extraction · Embedding · Indexing
```

The **Project** is the security and business boundary. This is a
single-organization deployment: every contract-derived row carries a
`project_id` and is filtered by it on every read. Cross-project retrieval is
prohibited except for an explicitly authorized System Admin query.

---

## Quick start

Requirements: **Podman** (or Docker) with its compose plugin. Nothing else needs
to be installed locally — no Python, no Node, no Postgres.

```bash
make podman-init          # start the podman machine (macOS/Windows); no-op on Linux
cp .env.example .env      # or: make env
make up                   # build + start the full stack
make logs                 # watch it come up
```

`make up` uses Podman when it is on PATH and falls back to Docker otherwise;
`make engine` prints which one it picked, and `make up ENGINE=docker` forces the
other. Every target in the Makefile follows the same detection.

| Service | URL |
| --- | --- |
| Frontend | http://localhost:5173 |
| API + Swagger | http://localhost:8000/docs |
| API ReDoc | http://localhost:8000/redoc |
| Queue introspection | http://localhost:9100/queues |
| MinIO console | http://localhost:9001 |
| Prometheus | http://localhost:9090 |
| Grafana | http://localhost:3001 |
| Jaeger traces | http://localhost:16686 |

Seeded system administrator:

```
admin@irisregtech.com  /  Abc@1234
```

Change it immediately outside local development. `make nuke` tears everything
down including volumes.

### Embeddings — NVIDIA Nemotron 3 Embed 1B

The platform's embedding model is `nvidia/nemotron-3-embed-1b`, served over an
NVIDIA NIM endpoint (hosted, or self-hosted to keep contract text inside your
network).

| | |
| --- | --- |
| Provider | `EMBEDDING_PROVIDER=nvidia` |
| Model | `nvidia/nemotron-3-embed-1b` |
| Dimension | **2048** — the model's real output width, verified against the provider at startup |
| Storage | **`halfvec`**, not `vector` — see below |
| Similarity | cosine (output is L2-normalised, so cosine and dot product agree) |
| Max input | 32,768 tokens; longer text is chunked upstream |

```bash
export EMBEDDING_PROVIDER=nvidia
export NVIDIA_API_KEY=nvapi-...
# Self-hosted NIM:
# export NVIDIA_BASE_URL=http://nim.internal:8000/v1
cip embeddings          # probe the provider and print the configuration
```

#### Why `halfvec` and not `vector`

This is the one non-obvious decision in the vector store, so it is worth stating
plainly: **pgvector's HNSW index supports at most 2000 dimensions for the `vector`
type, and this model emits 2048.** A `vector(2048)` column is accepted by Postgres,
accepts inserts, and then cannot carry an HNSW index at all — every similarity
search silently falls back to a sequential scan over the whole table. Nothing
errors; the system just gets slower and slower as the corpus grows.

`halfvec` indexes to 4000 dimensions and stores in half precision, so the column is
*smaller* than the 1536-d `vector` it replaces (4096 bytes vs 6144). For
L2-normalised embeddings compared against each other, fp16 rounding is far below
the margin separating a relevant hit from an irrelevant one.

Startup validation refuses to boot on `EMBEDDING_STORAGE=vector` with
`EMBEDDING_DIM > 2000`, precisely because the failure is otherwise invisible.

#### Query and passage prefixes

Nemotron 3 Embed is **asymmetric**: it is trained with `query:` before a search
string and `passage:` before a document. The provider applies these, and the
retrieval path calls `embed_query()` while the indexing path calls `embed_many()`.
Getting this backwards raises nothing and returns a perfectly valid vector — it
just sits in the wrong part of the space, and recall drops with no error to
explain it.

#### Matryoshka truncation

`EMBEDDING_DIM` may be lowered to 1024 or 512. The provider slices the leading
prefix and **re-normalises** (mandatory — a slice of a unit vector is not unit
length, and mixing sliced with unsliced makes the sliced ones score systematically
lower). Anything above 2048 is rejected at startup: vectors are never padded.

#### Changing the embedding model

Vectors from two models are not comparable, so a partially-migrated index is worse
than an empty one — it keeps answering, from whichever space happens to score
higher. The procedure:

```bash
cip migrate                      # widens/retypes the column, discards stale vectors
cip reindex-embeddings --all     # regenerates them through the normal pipeline
```

`cip reindex-embeddings` is **resumable and idempotent**: the work list is derived
from the database (contracts holding a vector that is not the target model), so an
interrupted run picks up where it stopped and a second run is a no-op. Contracts
are queued from the `embedding` stage, so parsing/chunking/extraction keep their
checkpoints, progress appears on the Processing screen, and retries behave as they
do for any job. Semantic search is degraded for a contract until its re-index
completes; keyword search is unaffected.

See [`docs/embedding-migration.md`](docs/embedding-migration.md) for the full
procedure and troubleshooting.

### Running without AI credentials

`LLM_PROVIDER=mock` and `EMBEDDING_PROVIDER=mock` are the defaults, and neither
the `anthropic` nor the `openai` package is installed in the base image. The
**entire pipeline runs end to end with no vendor account and no API key** —
upload, parse, classify, chunk, extract, embed, index, search, Copilot and export.

Be clear about what that gives you. The mock provider is deterministic and
schema-conformant, which makes it right for tests, CI and UI work — but it
**synthesises** its output rather than reading the contract, and mock embeddings
are not semantically meaningful. Clause attributes, risk scores and semantic
search are therefore demo-grade, not analysis you can rely on. The API logs a
`mock_ai_providers_active` warning on every start while either is in use, and
`APP_ENV=production` refuses to boot on mock unless `ALLOW_MOCK_AI=true` records
the decision deliberately.

For real extraction quality, install the extra and point the providers at a
vendor — nothing else changes:

```bash
pip install '.[ai]'
export LLM_PROVIDER=anthropic ANTHROPIC_API_KEY=sk-...
export EMBEDDING_PROVIDER=openai OPENAI_API_KEY=sk-...
```

Selecting a provider whose package is absent fails at the first call with an
actionable message (`Anthropic support requires the 'ai' extra: pip install
'.[ai]'`), not an ImportError traceback.

---

## Repository layout

```
backend/            FastAPI application (all business logic lives here)
  app/core/         config · logging · security · telemetry · errors · deps
  app/db/           async engine · session · declarative base
  app/models/       SQLAlchemy models
  app/schemas/      Pydantic v2 request/response contracts
  app/api/v1/       routers, one per domain
  app/services/     domain services
  app/repositories/ data access (repository pattern)
  app/ai/           parsers · cdm · enrichment · classification · profiles ·
                    chunking · extraction · embedding · graph · retrieval ·
                    context · prompt · rag
  app/orchestrator/ workflow engine · execution engine · stage registry
  app/workers/      internal stage endpoints invoked by the queue shim
  app/storage/      IObjectStorage + Azure/S3/MinIO/local adapters
  app/export/       xlsx (now) · csv · json · pdf (drop-in)
  migrations/       Alembic versions
  tests/            unit + integration
queue/              BullMQ dispatch shims (Node) — no business logic
frontend/           React 18 + Vite + TS + Tailwind + shadcn/ui
infra/              otel collector · prometheus · grafana · postgres init
```

---

## The processing pipeline

One job per contract. Each stage emits a **reusable artifact** that doubles as a
checkpoint, so a failure resumes at the failed stage instead of restarting.

| # | Stage | Artifact | Pool |
| --- | --- | --- | --- |
| 1 | Validation | `validation.json` | Validation |
| 2 | Parser | `normalized_document.json` | Parser |
| 3 | Enrichment | `canonical_document.json` | Enrichment |
| 4 | Classification | `classification.json` → selects a DIP | Classification |
| 5 | Chunking | `chunks.json` | Chunk |
| 6 | AI Extraction | `clauses/entities/obligations/risks/timelines/relationships.json` | LLM |
| 7 | Embedding | `summary/clause/chunk_embeddings.json` | Embedding |
| 8 | Indexing | search index + knowledge graph → `READY` | Index |

```
QUEUED → VALIDATING → PARSING → AI_EXTRACTION (docpipeline, then extraction)
       → EMBEDDING → INDEXING → READY
alt: FAILED · RETRYING · CANCELLED · PAUSED
```

**Stage rules.** Independently retryable · retry only the failed stage, never
completed ones · checkpoint after each success · **idempotent** (re-running a
stage never duplicates rows or vectors) · repository size must never slow
ingestion.

Re-run a stage and everything after it:

```bash
make reprocess job=<job-uuid> stage=embedding
```

### Parser agnosticism

Every parser implements `IDocumentParser` and emits a **Normalized Document**;
the CDM builder turns that into the immutable **Canonical Document Model**.
Downstream code never sees parser JSON. Switching parsers is a config change
(`ACTIVE_PARSER=idoc|pymupdf|adi|…`) plus one adapter — chunking, extraction,
embedding, search and RAG are untouched.

### Document Intelligence Profiles

A DIP is the configuration-driven brain for a document type: extraction prompts
and required clauses, chunking strategy, embedding levels, risk mapping,
compliance rules, validation rules, review triggers. **No document rules are
hardcoded** — a new contract type is a new profile, zero code changes. Profiles
are versioned, and a contract stays linked to the profile version used at
processing time.

---

## The retrieval and answer path

```
Query → Auth → Project + RBAC → Metadata filter → Retrieval Planner
      → (metadata | summary vectors | clause vectors | chunk vectors |
         graph traversal | hybrid)
      → Cross-encoder re-rank → Evidence Package
      → Context Assembly Engine → Prompt Orchestrator
      → RAG Engine (LLM) → Response Validator
      → Answer + citations → PDF highlight in the viewer
```

Metadata filters run **before** vector search, so a repository of millions of
contracts still answers fast. Hybrid order: metadata → keyword (`ts_rank`) →
vector ANN (HNSW) → cross-encoder re-rank.

Retrieval, context assembly, prompting and inference are four separate layers
with one-way dependencies: the RAG engine never retrieves and never builds
prompts, it only consumes a validated Context Package.

Every answer segment carries citations — document id, clause id, chunk id, page,
bounding box, confidence, artifact version — which the viewer renders as
highlight overlays on the source PDF.

---

## Core design rules

1. **Parser agnostic** — no vendor dependency escapes an adapter.
2. **Canonical Document Model** — one internal schema; immutable once built.
3. **Stage isolation** — independently scalable, retryable, replaceable, versioned.
4. **Artifact-driven** — every stage output is a checkpoint and is reusable.
5. **Incremental processing** — a version change regenerates only affected stages.
6. **Metadata-first retrieval** — filter before you search.
7. **Hierarchical retrieval** — document → clause → chunk.
8. **Horizontal scalability** — stateless worker pools.
9. **Version everything** — parser, CDM, chunk strategy, prompt, model, profile, index.
10. **Enterprise observability** — structured logs, metrics, traces, health checks.
11. **Project isolation** — every derived row is scoped by `project_id`.
12. **Explainable AI** — every extraction and answer carries evidence.

---

## Configuration

### One database, every environment

A laptop, the dev server and CI all read and write **Hackathon-DB-SRV**. It has
two addresses and they are the same PostgreSQL instance, not a copy:

| | |
| --- | --- |
| Public | `35.154.17.203:5432` — from a laptop |
| Private | `172.15.151.102:5432` — from the dev server, `13.234.93.157` |
| Database / schema | `team-1` / `clear` (`DB_SCHEMA`) |
| `system_identifier` | `7667820444287067012` |

This is not a convention to be tidied up later. The `cip_*` tables that hold the
clause taxonomy are maintained by another system and exist only here, so a stack
pointed anywhere else does not fail in a way that names the cause — it
classifies a document, finds no clause list for the type, and reports *"No
clauses are defined for document type"* as though the taxonomy were wrong.

Confirm what a stack actually reached rather than what you believe you set:

```bash
make db-target                                # asserts Hackathon-DB-SRV, exits non-zero if not
podman exec cipdemo-backend printenv DATABASE_URL
```

Two things that make a misconfiguration look like a working system:

- `docker-compose.yml` falls back to a bundled `postgres` service when
  `DATABASE_URL` is **unset**, so a missing value starts cleanly against an
  empty database. That service stays running because two others declare
  `depends_on` on it; it holds no application data. `make psql-local` reaches
  it, `make psql` reaches the real one.
- Comparing hostnames proves nothing — two environments can hold the same string
  and reach different servers. `system_identifier` is the only value that
  settles it, which is what `make db-target` compares.

### Everything else

All configuration is environment-based (`pydantic-settings`); no secrets in code.
`.env.example` documents every variable. The ones that change behaviour most:

| Variable | Purpose |
| --- | --- |
| `ACTIVE_PARSER` | `idoc` (default) · `pymupdf` · `adi` · `textract` · `googledocai` |
| `LLM_PROVIDER` / `LLM_MODEL` | inference provider and model |
| `EMBEDDING_PROVIDER` / `EMBEDDING_MODEL` / `EMBEDDING_DIM` | vector provider |
| `STORAGE_PROVIDER` | `azure` · `s3` · `minio` · `local` |
| `QUEUE_DRIVER` | `bullmq` (default) · `arq` (pure-Python alternative) |
| `RERANKER_ENABLED` | cross-encoder re-rank on/off |
| `CONTEXT_TOKEN_BUDGET` | context assembly token ceiling |
| `REVIEW_CONFIDENCE_THRESHOLD` | human-review trigger (default 0.85) |
| `ALERT_EXPIRY_WINDOW_DAYS` | expiring-contract alert window |

---

## Development

```bash
make lint            # ruff + mypy + eslint + tsc across all three packages
make format          # auto-format
make test            # backend (pytest) + frontend (vitest)
make test-integration
make migration m="add clause synonyms"   # autogenerate a migration
make migrate
make psql
make openapi         # dump openapi.json
```

Standards: SOLID · clean architecture · repository pattern · DI via FastAPI
`Depends` · async I/O throughout · env-based config · explicit exception
handling · unit + integration tests. No placeholder implementations.

### Queue note

The prompt requires BullMQ (Node) while all business logic must be Python. The
resolution: **BullMQ is a logic-free dispatch layer.** Each Node worker pulls a
job and calls `POST /internal/stages/{stage}/run` on the Python service, which
does the work, writes the artifact and returns the result. The worker→service
contract is deliberately trivial so the queue is swappable — set
`QUEUE_DRIVER=arq` for a pure-Python queue over the same Redis with no change to
stage code.

---

## Observability

- Structured JSON logs with correlation/trace ids on every record.
- Prometheus metrics at `/metrics` for the API, each worker pool and the queue.
- OpenTelemetry traces spanning API → queue → worker → provider calls.
- `GET /healthz` (liveness) and `GET /readyz` (dependency readiness).
- Pipeline metrics: queue depth, stage durations, retries, worker utilisation,
  throughput, cost. Retrieval metrics: latency, strategy mix, candidate counts,
  re-rank latency, cache hit rate. RAG metrics: latency, tokens, cost, citation
  rate, regeneration rate.

---

## Deployment

Deployment is container-based, on **Podman or Docker** — the same
`docker-compose.yml` runs on both, and `make up` selects Podman when it is on
PATH. Check which engine you have with `make engine`.

```bash
make podman-init      # start the podman machine (macOS/Windows); no-op on Linux
make up               # build and start the whole stack
make embedding-check  # probe the embedding provider before ingesting anything
```

Podman is preferred where available because it is rootless by default: a
compromised container is confined to an unprivileged user rather than to root on
the host, which is the right default for a stack holding contract text.

Two Podman-specific details are already handled in the compose file — bind mounts
carry `:z` so SELinux hosts can read `./infra`, and every published port is above
1024 so rootless Podman can bind them. Both are inert under Docker.

Scale a worker pool independently:

```bash
podman compose up -d --scale worker-ai=4
```

CI/CD (`.github/workflows/ci.yml`) runs lint → type-check → migrate against a real
Postgres → test → interface-contract checks → image build.

---

## Further reading

| Document | Covers |
|---|---|
| [`docs/BACKEND_GUIDE.md`](docs/BACKEND_GUIDE.md) | End-to-end walkthrough of the backend in plain language, with flow diagrams. Start here. |
| [`docs/DATABASE_SCHEMA.md`](docs/DATABASE_SCHEMA.md) | Every table, column, enum, index and trigger. |
| [`docs/ai-pipeline-reliability.md`](docs/ai-pipeline-reliability.md) | Model routing and tiers, retry/timeout behaviour, embedding-space validation, classification fallback reasons, chunk rejection diagnostics, and the operator commands for each. |
| [`docs/embedding-migration.md`](docs/embedding-migration.md) | Changing embedding model or dimension. |
| [`docs/artifact-persistence.md`](docs/artifact-persistence.md) | How stage artifacts are stored, versioned and superseded. |
| [`docs/alerting.md`](docs/alerting.md) | Alert rules, evaluation and delivery. |

Operator commands that come up most often:

```bash
cip embeddings                       # embedding config + vector-space consistency
cip reindex-embeddings [--all]       # re-embed into the configured space
cip replay-chunking <id> [--sweep]   # test chunk thresholds; writes nothing
python -m scripts.benchmark_routing  # model routing cost, before vs after
python scripts/audit_secrets.py      # credential scan over every tracked file
```
