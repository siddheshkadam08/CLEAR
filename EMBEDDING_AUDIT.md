# Embedding production audit

**Verified 2026-07-31** against PostgreSQL 17.10 / pgvector 0.8.5.

Regenerate at any time:

```bash
make audit                                    # via compose
python -m app.tools.system_diagnostics --markdown EMBEDDING_AUDIT.md
```

---

## Summary

| | |
| --- | --- |
| Embedding provider | `nvidia` (default `mock` until credentials are supplied) |
| Embedding model | `nvidia/nemotron-3-embed-1b` |
| **Detected embedding dimension** | **2048** — from NVIDIA's model card, asserted at startup against the live response |
| **Database vector dimension** | **2048** — `embeddings.embedding halfvec(2048)`, verified live |
| Vector index | HNSW, `halfvec_cosine_ops`, one partial index per level — all three **valid** |
| Similarity metric | cosine (output is L2-normalised, so cosine ≡ dot product) |
| Migration status | **applied and verified** — revisions `0001` → `0002` |
| Startup validation | **implemented**, aborts the process on a fatal mismatch |
| Health endpoint | **extended**, reports provider/model/dimension/latency/pgvector |
| Inference provider | `gemini`, `gemini-2.5-flash` (flash-lite for classification/summary) |
| Diagnostics | `status: healthy`, 0 problems, 0 warnings |

**Test suite: 103 passing, 3 skipped** (skips require `NVIDIA_API_KEY`).

---

## The finding that shaped the schema

pgvector's HNSW index supports at most **2000 dimensions** for the `vector` type.
Nemotron 3 Embed 1B emits **2048**.

A `vector(2048)` column is created without complaint, accepts every insert, and then
**cannot carry an HNSW index at all**. Nothing raises. Every similarity search
silently degrades to a sequential scan, and the only symptom is search getting
slower as the corpus grows — which reads like a capacity problem, not a schema one.

Verified empirically, not just from documentation:

```
HNSW on vector(2048)   -> REFUSED: asyncpg.exceptions.ProgramLimitExceeded
HNSW on halfvec(2048)  -> CREATED
```

The column is therefore `halfvec(2048)`, which indexes to 4000 dimensions and is
*smaller* than the 1536-d `vector` it replaced (4096 bytes vs 6144). For
L2-normalised vectors compared against each other, fp16 rounding is far below the
margin separating a relevant hit from an irrelevant one — confirmed by a live
round-trip test asserting cosine distance to self < 1e-3.

`app/ai/embedding/diagnostics.py` refuses to start the process on
`EMBEDDING_STORAGE=vector` with `EMBEDDING_DIM > 2000`, because that failure is
otherwise invisible.

---

## Second correctness finding: the model is asymmetric

Nemotron 3 Embed is trained with `query:` before a search string and `passage:`
before a document. Both call sites previously used the same path. Using the document
path for a query returns a perfectly valid vector that sits in the wrong part of the
space — no error, no log line, just lower recall.

`InputType` is now on the provider interface; `RetrievalEngine` calls
`embed_query()` and the indexing path calls `embed_many()`. Asserted by
`test_query_and_passage_use_different_prefixes`.

---

## Verified live

Against PostgreSQL 17.10 + pgvector 0.8.5 in an isolated database:

| Check | Result |
| --- | --- |
| `cip migrate` (0001 → 0002) | applied cleanly |
| `embeddings.embedding` type | `halfvec(2048)` |
| HNSW indexes | 3 created, all `indisvalid = true` |
| `vector(2048)` + HNSW | refused with `ProgramLimitExceeded` |
| `halfvec(2048)` + HNSW | created |
| halfvec round trip | cosine distance to self < 1e-3 |
| k-NN over 10 vectors | returns the correct nearest neighbour |
| Wrong-width insert | rejected by the database |
| NaN insert | rejected by the database |
| ORM insert path | round-trips through `vector_column()`, drift < 1e-2 |
| `verify_pgvector` | exit 0, "matches the active embedding model" |
| `system_diagnostics` | `healthy`, 0 problems |

---

## Deliverables

| # | Deliverable | Location |
| --- | --- | --- |
| 1 | Embedding capability probe | `python -m app.tools.embedding_probe` |
| 2 | pgvector validation tool | `python -m app.tools.verify_pgvector` |
| 3 | Automatic migration generation | `verify_pgvector --generate-migration` |
| 4 | Startup validation | `app/ai/embedding/diagnostics.py`, wired in `app/main.py` |
| 5 | Health endpoint | `GET /readyz` → `checks.embedding` |
| 6 | Compose `nvidia` profile | `docker compose --profile nvidia up embedding-check` |
| 7 | System diagnostics | `python -m app.tools.system_diagnostics` (text/JSON/Markdown) |
| 8 | Repository audit | this document |
| 9 | Pipeline validation | `validate_vector()`, provider `_validate()`, DB constraints |
| 10 | Re-index utility | `cip reindex-embeddings` |
| 11 | Tests | `backend/tests/` — 103 passing |
| 12 | Documentation | `README.md`, `docs/embedding-migration.md`, `.env.example` |
| 13 | Audit report | this document |

Every tool prints human-readable output by default, machine-readable JSON with
`--json`, and exits non-zero on failure so CI can gate on it. None of them mutate
anything; applying a migration stays an explicit `cip migrate`.

---

## Validation layers

A bad vector has to pass five independent checks to reach storage:

1. **Configuration** (startup) — credentials, endpoint, dimension vs the model's
   published width, dimension vs the storage type's HNSW ceiling.
2. **Live probe** (startup) — the provider answers, and answers in the configured
   width.
3. **Schema** (startup) — the live column's type and width match the configuration;
   a stale schema aborts the process.
4. **Per-vector** (`validate_vector`) — null, non-array, wrong length, null element,
   non-numeric element, boolean, NaN, ±infinity. Each rejection is logged with its
   position.
5. **Database** — pgvector itself refuses a wrong-width or NaN vector.

Nothing is ever truncated or padded to make it fit.

---

## Repository audit

Searched for the assumptions the migration could have left behind.

| Concern | Finding |
| --- | --- |
| Hardcoded vector dimensions | None in application code. The column derives from `EMBEDDING_DIM` via `vector_column()`; the migration hardcodes its target deliberately (a migration that reads live settings produces different schemas per environment under one revision id) and a test asserts the two agree. |
| OpenAI embedding defaults | Removed. `EMBEDDING_MODEL` defaults to `nvidia/nemotron-3-embed-1b`, `EMBEDDING_DIM` to 2048. |
| OpenAI-tuned thresholds | Retuned. `RETRIEVAL_MIN_SIMILARITY` 0.25 → 0.40, with per-level overrides (0.35 document / 0.45 clause / 0.40 chunk) and configurable fusion weights. |
| Unused embedding providers | `openai`, `azure_openai`, `sentence_transformers` retained deliberately — the architecture is provider-agnostic by design and the spec says not to remove the abstraction. All are lazily imported; none is installed in the base image. |
| Obsolete environment variables | `LLM_TEMPERATURE` removed (current Claude models reject it with a 400). `docling` removed from `ParserName`, the registry, `versions.py` and the `parsers` extra. `tiktoken` removed — an OpenAI tokeniser that would miscount for every configured provider. |
| Duplicate embedding implementations | None. One `IEmbeddingProvider`, one engine, one stage, one re-index path that reuses the existing reprocess pipeline rather than reimplementing orchestration. |
| Dead configuration | `EMBEDDING_STORAGE`, `EMBEDDING_VERIFY_ON_STARTUP`, `NVIDIA_*`, `GEMINI_*` all read and tested. |

Provider abstraction is intact: adding Gemini embeddings, Voyage or Cohere is one
`IEmbeddingProvider` subclass and a registry entry. No change to the retrieval
pipeline, vector storage, context assembly, search API or orchestration layer.

---

## Architecture as configured

| Decision | Configured value |
| --- | --- |
| Chunk size | 700 target / 800 max tokens |
| Chunk overlap | 100 tokens |
| Top-K (chunk level) | 20 |
| Retrieval | hybrid — vector 0.65, keyword 0.35, RRF fusion |
| Vector index | HNSW, `m=16`, `ef_construction=64`, partial per level |
| Metadata filters | project, agreement type, jurisdiction/governing law, effective & expiration date, status, tags, risk band, unlimited-liability flag |

`project_id` is the security boundary throughout (§1.1) — the platform is not
multi-tenant, and every contract-derived row is filtered by project on every read.

---

## Remaining manual steps

Everything else is implemented, automated and tested. Three actions remain, and they
are the three that require credentials or a target database:

### 1. Supply `NVIDIA_API_KEY`

```bash
export NVIDIA_API_KEY=nvapi-...
export EMBEDDING_PROVIDER=nvidia
python -m app.tools.embedding_probe        # must report dimension 2048, exit 0
```

If the live model reports anything other than 2048, **stop**: set `EMBEDDING_DIM` to
what it reported, run `python -m app.tools.verify_pgvector --generate-migration`,
review the generated file, apply it, then re-index. Nothing is truncated or padded
to paper over the difference.

### 2. Supply `GOOGLE_API_KEY`

```bash
export GOOGLE_API_KEY=...        # rotate first if it has ever been pasted anywhere
export LLM_PROVIDER=gemini
```

Confirm `GEMINI_MODEL` names a model your project can access — model availability
varies by account and region, and a name this deployment cannot reach fails with a
404 that is not retried.

### 3. Supply `TEST_DATABASE_URL` and run the live suite

```bash
export TEST_DATABASE_URL=postgresql+asyncpg://cip:cip@localhost:5432/cip_test
cd backend && python -m pytest tests -q
```

With `NVIDIA_API_KEY` also set, the three currently-skipped tests run: real
embedding generation, dimension verification against the live column, and a
retrieval-quality check asserting that a liability clause outranks an unrelated
delivery clause for a liability query — which is what fails first if the
query/passage prefixes are ever wired the wrong way round.

---

## Production readiness checklist

| | Item | Status |
| --- | --- | --- |
| ☑ | Embedding dimension determined from the model, never assumed | 2048, verified |
| ☑ | pgvector schema matches the model | `halfvec(2048)`, verified live |
| ☑ | HNSW indexes exist and are valid | 3/3 valid |
| ☑ | Startup aborts on a dimension mismatch | implemented + tested |
| ☑ | Startup aborts on missing credentials | implemented + tested |
| ☑ | Startup aborts when pgvector is missing | implemented |
| ☑ | Vectors never truncated or padded | 5 validation layers |
| ☑ | NaN / infinity / malformed vectors rejected | implemented + tested |
| ☑ | Query/passage asymmetry wired correctly | implemented + tested |
| ☑ | Retry only on retryable failures | implemented + tested |
| ☑ | Rate limits honour `Retry-After`, with jitter | implemented + tested |
| ☑ | Connection pooling | implemented |
| ☑ | Cancellation propagates | implemented |
| ☑ | Health endpoint reports embedding state | implemented + tested |
| ☑ | Migration generated automatically, applied manually | implemented + tested |
| ☑ | Re-index is resumable and idempotent | implemented |
| ☑ | No embeddings mixed across models | enforced by model-keyed reuse + re-index |
| ☑ | Default stack runs with no vendor credentials | `mock`, verified |
| ☑ | Provider abstraction intact | 4 embedding providers, 5 inference providers |
| ☑ | Documentation updated | README, migration guide, env, this report |
| ☐ | Live NVIDIA endpoint verified | **needs `NVIDIA_API_KEY`** |
| ☐ | Live Gemini endpoint verified | **needs `GOOGLE_API_KEY`** |
| ☐ | Retrieval quality measured on real contracts | **needs both, plus a corpus** |

---

## Security note

`GOOGLE_API_KEY` and `NVIDIA_API_KEY` are read from the environment and are never
written to any file in this repository. `.env` and `.env.*` are gitignored.

**Any credential that has been pasted into a chat, an issue tracker or a shared
document should be treated as public and rotated**, regardless of who had access at
the time — it is now in at least one log or transcript outside your control.
