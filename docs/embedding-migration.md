# Embedding migration guide

How to move the vector store to `nvidia/nemotron-3-embed-1b`, and what to do when
it goes wrong.

---

## What changes

| | Before | After |
| --- | --- | --- |
| Provider | `openai` | `nvidia` |
| Model | `text-embedding-3-small` | `nvidia/nemotron-3-embed-1b` |
| Dimension | 1536 | **2048** |
| Column type | `vector(1536)` | **`halfvec(2048)`** |
| HNSW operator class | `vector_cosine_ops` | `halfvec_cosine_ops` |
| Input handling | symmetric | **asymmetric** (`query:` / `passage:`) |
| Similarity floor | 0.25 | 0.40 global, 0.35/0.45/0.40 per level |

Everything except the vector store is untouched. Contracts, chunks, clauses,
extractions, risks and obligations survive the migration; only the derived vectors
are regenerated.

---

## Why the column type changes

pgvector's HNSW index supports at most **2000 dimensions** for the `vector` type.
The model emits **2048**.

A `vector(2048)` column is created without complaint, accepts every insert, and
then cannot carry an HNSW index. Nothing raises. Similarity search silently becomes
a sequential scan over the entire table, and the only symptom is that search gets
slower as the corpus grows — which reads like a capacity problem, not a schema one.

`halfvec` indexes to 4000 dimensions and stores at half precision. The resulting
column is *smaller* than the one it replaces (2048 × 2 = 4096 bytes, versus
1536 × 4 = 6144), and for L2-normalised vectors compared against each other the
fp16 rounding is far below the margin that separates a relevant hit from an
irrelevant one.

Startup validation refuses to boot on a configuration that cannot be indexed.

---

## Procedure

### 1. Configure

```bash
EMBEDDING_PROVIDER=nvidia
EMBEDDING_MODEL=nvidia/nemotron-3-embed-1b
EMBEDDING_DIM=2048
EMBEDDING_STORAGE=halfvec
NVIDIA_API_KEY=nvapi-...
# Self-hosted NIM:
# NVIDIA_BASE_URL=http://nim.internal:8000/v1
```

### 2. Verify before touching the database

```bash
cip embeddings
```

Prints the configuration, probes the provider, compares the reported width against
`EMBEDDING_DIM` and against the live column, and lists any vectors still belonging
to another model. Fix anything marked `FAIL` before continuing.

### 3. Migrate

```bash
cip migrate
```

Revision `0002`:

1. checks that pgvector ≥ 0.7.0 (when `halfvec` arrived),
2. reports how many vectors are about to be discarded,
3. drops the three per-level HNSW indexes,
4. **deletes every existing vector**,
5. retypes the column to `halfvec(2048)`,
6. recreates the indexes with `halfvec_cosine_ops`.

Step 4 is not optional and not recoverable. A 1536-d vector from a different model
cannot be widened to 2048 — padding would invent dimensions the model never
produced, and keeping both is worse than deleting: vectors from two models occupy
unrelated spaces, so a mixed index returns confident nonsense rather than failing.

The rows are fully derived. Every one is regenerable from contract text still in
object storage and chunks still in Postgres.

### 4. Re-index

```bash
cip reindex-embeddings --all
```

Or scoped, to spread the load:

```bash
cip reindex-embeddings --project-id <uuid> --limit 100
cip reindex-embeddings --dry-run          # count only, changes nothing
```

**Resumable** — the work list is a query (contracts holding a vector that is not
the target model), not a cursor file, so it cannot disagree with reality. Interrupt
it and re-run; it picks up what is left.

**Idempotent** — a second run over completed work finds nothing to do.

**Observable** — contracts are queued through the normal pipeline from the
`embedding` stage, so each appears on the Processing screen with per-stage progress,
and failures land in the DLQ like any other job. Parsing, chunking and extraction
keep their checkpoints, so this costs one provider pass, not eight.

### 5. Confirm

```bash
cip embeddings          # "All vectors are nvidia/nemotron-3-embed-1b"
curl -s localhost:8000/readyz | jq .checks.embedding
```

---

## During the re-index

Semantic search returns nothing for contracts not yet re-embedded. Keyword search,
clause browsing, extraction, exports and the contract screens are unaffected —
hybrid search degrades to its keyword half rather than failing.

If that is unacceptable for your window, re-index project by project so each
completes quickly, rather than running one long sweep across everything.

---

## Troubleshooting

### `EMBEDDING_PROVIDER=nvidia but NVIDIA_API_KEY is empty`

Startup validation, before anything runs. Set the key. A self-hosted NIM without
authentication can use any non-empty placeholder.

### `EMBEDDING_STORAGE=vector can carry an HNSW index up to 2000 dimensions…`

The configuration would produce an unindexable column. Set
`EMBEDDING_STORAGE=halfvec`, or reduce `EMBEDDING_DIM` to 1024 (Matryoshka
truncation, which this model supports).

### `returned 2048 dimensions but EMBEDDING_DIM is 1536`

The provider works and its output does not fit the configuration. Set
`EMBEDDING_DIM=2048`, migrate, re-index. Nothing is truncated or padded to hide
this.

### `embeddings.embedding is vector(1536) but EMBEDDING_DIM is 2048`

The migration has not been applied to this database. Run `cip migrate`. Until then
every insert would be rejected.

### `The vector index still holds embeddings from text-embedding-3-small`

A warning, not fatal. The migration ran but the re-index has not finished. Run
`cip reindex-embeddings --all`. Results for un-migrated contracts are unreliable
until it completes.

### `pgvector 0.6.x does not support halfvec`

Upgrade the extension to ≥ 0.7.0 (the `pgvector/pgvector:pg16` image ships a
current build). If you cannot, set `EMBEDDING_STORAGE=vector` **and**
`EMBEDDING_DIM=1024` — 2048 on a `vector` column is not a supported configuration.

### `NVIDIA does not recognise the model … at <url>`

A 404 from the endpoint. Check `EMBEDDING_MODEL` spelling and that
`NVIDIA_BASE_URL` points at an endpoint serving that model. Not retried — it would
fail identically every time.

### Search quality dropped after migrating

Check that the re-index actually finished (`cip embeddings` lists vectors by
model). If it did, the similarity floors are the next thing to look at: Nemotron's
score distribution is not OpenAI's, and the defaults
(`RETRIEVAL_MIN_SIMILARITY=0.40`, with per-level overrides) are tuned for it. Lower
`RETRIEVAL_MIN_SIMILARITY_CLAUSE` if relevant clauses are being cut; raise it if
boilerplate is crowding the results.

---

## Rolling back

`cip downgrade 0001` returns the column to `vector(1536)` and is **equally
destructive** — 2048-d vectors do not fit, and slicing them in SQL would produce a
differently-scaled space (Matryoshka truncation requires re-normalisation, which
SQL will not do). Re-point the configuration at the old model and re-index again.
