# Document processing pipeline — how a contract becomes searchable knowledge

What happens between a user pressing Upload and a contract reaching `READY`. Every
constant and stage name below is taken from the source, not from memory; the file
references let you check any of it.

---

## The flow

```text
Upload (API returns 202 in seconds — no parsing happens in the request)
   ├─ ZIP expanded — each member becomes its own contract, with its own hash and job
   ├─ DOCX → PDF via LibreOffice (the pipeline is PDF-only)
   ├─ stored + SHA-256 duplicate check
   └─ ONE processing_jobs row per file, enqueued in the SAME transaction as the
      contract rows — so a rolled-back upload cannot leave work queued for a
      contract that never existed
         │
         ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ [1] VALIDATION                                                              │
│                                                                             │
│ Re-checks the file inside the pipeline rather than trusting the upload:      │
│ type, size, structural integrity, page count.                               │
│                                                                             │
│ Why a stage and not just request-time validation: the upload endpoint must   │
│ answer in seconds, so anything slow belongs here — and a file can be         │
│ re-processed months later, or arrive by a path other than the HTTP upload,   │
│ so the pipeline's own gate has to hold on its own.                          │
└─────────────────────────────────────────────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ [2] PARSER          registry: idoc │ pdfextract │ pymupdf │ docx │ adi       │
│                                                                             │
│ The stage contains no parsing logic — that lives entirely in the adapter,    │
│ which is what makes switching parsers a configuration change.                │
│                                                                             │
│ Produces a NormalizedDocument plus per-page JSON carrying:                   │
│   • text                    • layout roles, including `sectionHeading`       │
│   • page references         • paragraph geometry (bounding boxes)            │
│                                                                             │
│ Cached at parser-cache/{parser}/{sha256} — a given PDF is parsed once, ever. │
│ Parser name and version are stamped on the checkpoint, so changing           │
│ ACTIVE_PARSER invalidates this stage and everything downstream.              │
└─────────────────────────────────────────────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ [3] DOCPIPELINE                                                             │
│     load pages → classify → look up clauses → detect → embed → persist      │
│                                                                             │
│ ┌─ 3a  CLASSIFY — first 5 pages only  (CLASSIFICATION_PAGE_WINDOW = 5)       │
│ │      "A contract announces itself in its title, recitals and definitions;  │
│ │       by page six it is reciting obligations that look much the same       │
│ │       whatever the instrument."                                            │
│ │                                                                            │
│ │      The label set is NOT hardcoded. It is read at runtime from            │
│ │      `document_profiles`, and the result is written to                     │
│ │      contracts.agreement_type. A classifier emitting anything else would   │
│ │      produce a document type with no clauses to look for.                  │
│ │                                                                            │
│ ├─ 3b  LOOK UP the clause list that document type expects                    │
│ │      document_profiles → mandatory_clauses / optional_clauses + Clause     │
│ │      Master. This is what makes "missing clause" a meaningful finding:     │
│ │      the profile says what *should* be there.                              │
│ │                                                                            │
│ ├─ 3c  DETECT CLAUSES — two passes, cheapest first. BOTH ARE LLM CALLS.      │
│ │                                                                            │
│ │      Pass A — section headings                                             │
│ │        A heading is a short, deliberate statement of what follows, so it   │
│ │        is the most precise signal available and nearly free.               │
│ │          1. exact match on normalised heading text — costs nothing         │
│ │          2. one LLM call for the remainder, carrying headings only,        │
│ │             never body text                                                │
│ │                                                                            │
│ │      Pass B — whatever Pass A missed                                       │
│ │        Searched in the body, four pages at a time.                         │
│ │          DEFAULT_CHUNK_PAGES        = 4                                    │
│ │          DEFAULT_CHUNK_OVERLAP      = 0                                    │
│ │          DEFAULT_CHUNK_CONCURRENCY  = 4   (each call is a 30–100s trip)    │
│ │          MIN_CHUNK_CHARS            = 200 (covers, signature pages and     │
│ │                                            exhibit dividers are skipped —  │
│ │                                            recorded as skipped, not        │
│ │                                            unread)                         │
│ │                                                                            │
│ │        A clause not found in one chunk stays outstanding and is carried    │
│ │        into the next, and the next, until either every clause is found or  │
│ │        the document runs out.                                              │
│ │                                                                            │
│ │        That loop rule is what gives the report its meaning. Stopping early │
│ │        *and* reporting clauses as missing would be a statement about how   │
│ │        far the search got, printed in a way that reads as a statement      │
│ │        about the contract.                                                 │
│ │                                                                            │
│ ├─ 3d  EMBED the text of each DETECTED clause (batched here, not in the      │
│ │      provider, so a hundred-clause document is not one enormous request)   │
│ └─ 3e  PERSIST clauses with page and bounding-box evidence                   │
└─────────────────────────────────────────────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ [4] EXTRACTION — typed knowledge, running on what docpipeline located        │
│                                                                             │
│ Obligations · Key dates · Risks · Parties                                   │
│ Each row carries its evidence: page, bounding box, confidence, and the       │
│ exact parser / prompt / model versions used.                                 │
│                                                                             │
│ Chunks are built from the parser's cached page JSON, grouped by the heading  │
│ that introduces them — not per paragraph. A paragraph-level chunk falls      │
│ below the engine's `min_tokens: 15` floor and carries no heading for the     │
│ matching rules to use, which is how an earlier version extracted zero        │
│ clauses from a document docpipeline had found twenty in.                     │
└─────────────────────────────────────────────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ [5] EMBEDDING — the three-level vector hierarchy that makes a contract       │
│                 findable                                                    │
│                                                                             │
│   L1  one vector per document (summary)                                     │
│   L2  one vector per extracted clause                                       │
│   L3  one vector per chunk                                                  │
│                                                                             │
│ Provider is configuration, not code. Currently:                             │
│   azure_openai · text-embedding-3-small · 1536-d · halfvec · HNSW           │
│                                                                             │
│ `halfvec` is required rather than preferred: pgvector's HNSW index stops at  │
│ 2000 dimensions for the `vector` type, so a wider column cannot be indexed   │
│ and every similarity search degrades to a sequential scan.                   │
└─────────────────────────────────────────────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ [6] INDEXING — resolve extracted references into real rows                  │
│                                                                             │
│ Extraction recorded what the text *says*; this turns those references into   │
│ edges between actual rows, in `knowledge_relationships`, and records the     │
│ ones that resolve to nothing.                                               │
│                                                                             │
│ Those edges are what RetrievalEngine._expand_graph traverses. Without this   │
│ stage retrieval still works and quietly sees a thinner graph — a regression  │
│ that shows up as slightly worse answers rather than an error.                │
└─────────────────────────────────────────────────────────────────────────────┘
         │
         ▼
       READY
```

---

## Dispatch between stages

Each stage commits its checkpoint **before** the next is queued. A crash between stages
resumes rather than restarts, and a fast worker cannot begin a stage before the
checkpoint it depends on is visible. Retries re-queue only the failed stage; completed
stages are never re-run.

The pipeline's sequencing lives in Python, in one place — the queue only delivers
messages.

---

## Document types are configuration, not code

Read at runtime from `document_profiles`. The thirteen currently configured:

```text
amendment                 healthcare_agreement      purchase_order
consulting_agreement      insurance_policy          research_collaboration
employment_agreement      lease                     vendor_agreement
government_contract       license_agreement
msa                       nda
```

Adding a document type is a profile row plus its clause list — **no code change**. This
is also why the classifier's label set cannot be written down in a diagram and stay true.

---

## What this corrects

An earlier hand-drawn flowchart circulated with five substantive errors. Recorded here so
anyone still holding it can see what changed.

| Earlier chart said | Actually |
|---|---|
| Clause not found → chunks → **"Vector Search + Semantic Matching"** | **Clause detection never uses vector search.** Both passes are LLM calls. `vectors.py` is *"Stage 3: embed the text of each **detected** clause"* — embeddings run *after* detection, for retrieval |
| A Yes/No branch — one path **or** the other | **Both passes always run.** Pass B searches for whatever Pass A missed, carrying outstanding clauses forward until found or the document is exhausted |
| Fixed list: NDA, MSA, **SOW**, Vendor, Employment, Other | Thirteen configurable profiles, read at runtime. **There is no SOW** |
| "Generate Embeddings (**OpenAI**)" — a single step | Provider is configurable, currently Azure OpenAI, and it is a **three-level hierarchy** (document / clause / chunk) |
| Ended at "Store in Database" | Three stages were missing: **validation** before parsing, **extraction** and **indexing** after |

Two smaller ones: "Section Header Detection" is not a stage — the parser emits
`role: "sectionHeading"` and clause detection consumes it. And "Clause Normalization" is
not a post-identification step — normalisation is text-matching *inside* Pass A; what
follows detection is mapping to the Clause Master taxonomy.

---

## Source references

| Fact | Where |
|---|---|
| Stage order | `STAGE_ORDER`, `backend/app/core/enums.py` |
| Chunk constants | `backend/app/ai/docpipeline/clauses.py` (lines 50–71) |
| Classification window | `CLASSIFICATION_PAGE_WINDOW`, `classification.py:34` |
| Pipeline sequence | `backend/app/ai/docpipeline/runner.py` |
| Clause taxonomy | `backend/app/ai/docpipeline/mapping.py` |
| Embedding levels | `backend/app/orchestrator/stages/embedding.py` |
| Graph edges | `backend/app/orchestrator/stages/indexing.py` |
