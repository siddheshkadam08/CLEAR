---
title: "CLEAR — Contract Intelligence Platform"
subtitle: "Backend Guide for the Team (Plain English Edition)"
author: "IRIS RegTech — Engineering"
date: "2026-07-31"
---

# How to read this document

This guide explains **everything our backend does**, written for a mixed audience:
engineers who did not build this part, product folks, QA, and anyone who has to
demo or support the system. Wherever something technical appears, there is a
plain-English explanation right next to it.

You do **not** need to read this top to bottom. Useful entry points:

| If you want to… | Read |
| --- | --- |
| Understand what the product does at all | Chapter 1 and 2 |
| Learn the vocabulary the team uses | Chapter 3 (Glossary) |
| Understand what happens when someone uploads a contract | Chapter 5 |
| Understand how a question gets answered | Chapter 6 |
| Understand who can see and do what | Chapter 7 |
| Know what is stored in the database | Chapter 8 |
| Find an API endpoint | Chapter 9 |
| Run it locally / debug it | Chapter 14 and 15 |

> **To turn this into a Word document**, run:
>
> ```
> pandoc docs/BACKEND_GUIDE.md -o CLEAR-Backend-Guide.docx --toc --toc-depth=3
> ```
>
> All diagrams in this file are plain text inside code blocks, so they survive the
> conversion and render in a monospace font in Word. Do not reflow them.

\newpage

# Chapter 1 — What this system actually is

## 1.1 The one-paragraph version

Companies sign thousands of contracts. Those contracts are PDFs and Word files
sitting in folders. Nobody can answer simple questions about them — *"which of our
contracts have unlimited liability?"*, *"what renews next quarter?"*, *"can we
terminate this one early?"* — without a human reading each document.

**CLEAR reads the contracts for you.** You upload a PDF; the system extracts the
structure and meaning, stores it in a searchable form, and then answers questions
about it — always showing you the exact page and paragraph the answer came from.

## 1.2 The three promises the system makes

Everything in the backend exists to keep one of these three promises.

**Promise 1 — "We will never make something up."**
Every fact the system shows you (a clause, a date, a risk, an answer from the
assistant) carries its **evidence**: which document, which page, which paragraph,
what the confidence was, and which version of which AI model produced it. If the
AI writes an answer citing a source that does not exist, the system detects it and
removes the fake citation before you ever see it.

**Promise 2 — "Your project's data never leaks into another project's."**
A **project** is the security wall. Every single row of extracted data carries the
project it belongs to, and every read is filtered by it. If you are not a member of
a project, the API pretends the project does not exist at all (it returns "not
found", not "forbidden" — because even confirming it exists is a leak).

**Promise 3 — "Nothing is a black box."**
Processing a contract is 8 separate, visible steps. You can see which step a
document is on, how long each step took, what it produced, and — when something
fails — exactly which step failed and why, in a sentence a human can act on.

## 1.3 What it is built with

| Layer | Technology | Why (in plain English) |
| --- | --- | --- |
| API | **Python + FastAPI** | All the business logic lives here. Fast, async, auto-generates its own API docs. |
| Database | **PostgreSQL 16 + pgvector** | Normal tables for contracts and clauses, *plus* the ability to store "meaning fingerprints" (vectors) and search by similarity — in one database. |
| Cache / sessions / queue backbone | **Redis** | Fast temporary memory: caches dashboards, tracks rate limits, and holds the job queue. |
| Job queue | **BullMQ (Node.js)** | A tiny Node service whose *only* job is handing work to Python workers. It contains zero business logic. |
| File storage | **Azure Blob / AWS S3 / MinIO / local disk** | The actual PDF bytes live here, never in the database. Swappable by config. |
| AI — reading & reasoning | **Gemini / Anthropic / OpenAI / self-hosted / mock** | Reads the contract language and answers questions. Swappable. |
| AI — search fingerprints | **NVIDIA Nemotron 3 Embed 1B** (default) | Turns text into numbers so we can find "similar meaning", not just "same words". |
| Monitoring | **Prometheus, Grafana, Jaeger, OpenTelemetry** | Charts, alerts, and the ability to trace one upload across every service it touched. |
| Frontend | **React 18 + Vite + TypeScript** | (Out of scope for this document, but it talks only to the API described here.) |

\newpage

# Chapter 2 — The big picture

## 2.1 The system map

```
                        ┌────────────────────────────────┐
                        │      Browser (React app)       │
                        │   Login · Upload · Search ·    │
                        │   Copilot · Dashboards         │
                        └───────────────┬────────────────┘
                                        │  HTTPS + JWT token
                                        ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                        FastAPI  —  the API Gateway                       │
│                                                                          │
│  Every request passes through, in this order:                            │
│   1. Request ID + logging context   (so we can trace it later)           │
│   2. Security headers + CORS        (browser safety)                     │
│   3. Body size limit                (reject giant uploads early)         │
│   4. Rate limit                     (stop abuse / runaway clients)       │
│   5. Authentication  → who are you?                                      │
│   6. Project access  → may you see this project at all?                  │
│   7. Permission      → may you do THIS specific action?                  │
│                                                                          │
│  Domains:  Auth · Users · Projects · Contracts · Processing ·            │
│            Knowledge · Search · Copilot · Dashboards · Alerts ·          │
│            Exports · Clause Master / Admin                               │
└───────┬───────────────────┬──────────────────────┬───────────────────────┘
        │                   │                      │
        ▼                   ▼                      ▼
┌───────────────┐   ┌───────────────┐   ┌────────────────────────────┐
│ PostgreSQL 16 │   │     Redis     │   │  Object storage            │
│  + pgvector   │   │ cache·session │   │  (Azure / S3 / MinIO /     │
│               │   │ ratelimit·    │   │   local disk)              │
│ • contracts   │   │ queue         │   │                            │
│ • clauses     │   └───────┬───────┘   │ • the original PDF/DOCX    │
│ • chunks      │           │           │ • every stage's artifact   │
│ • vectors     │           │           │ • generated Excel exports  │
│ • jobs, users │           │           └────────────────────────────┘
└───────────────┘           │
                            │
        ┌───────────────────┴─────────────────────┐
        │        BullMQ dispatcher (Node)         │
        │  "Postman". Takes a job off the queue,  │
        │  knocks on Python's door, reports back. │
        │  Knows NOTHING about contracts.         │
        └───────────────────┬─────────────────────┘
                            │  POST /internal/stages/{stage}/run
                            ▼
        ┌─────────────────────────────────────────┐
        │   Python worker pools (same codebase)   │
        │                                         │
        │   worker-parser →  stages 1–5           │
        │   worker-ai     →  stages 6–8           │
        │                                         │
        │   Scale each pool independently.        │
        └─────────────────────────────────────────┘
```

**In plain English:** the browser talks to one Python API. That API stores small
structured facts in Postgres, big files in object storage, and hands slow work to a
queue. A little Node service acts as a postman between the queue and the Python
workers that actually do the slow work.

## 2.2 Why the Node "postman" exists

This is the most-asked question about the architecture, so here it is plainly.

The project requirement was: **use BullMQ** (which is a Node.js library) **but keep
all business logic in Python**. Those two requirements fight each other.

Our resolution: BullMQ is used purely as a *delivery mechanism*. The Node worker:

1. picks a message off the queue,
2. sends an HTTP POST to Python: *"please run the 'chunking' stage for job X"*,
3. reads Python's reply and marks the queue job done, retried, or dead.

That is the whole file. The Node worker does not know what a contract is, what
chunking means, which stage runs next, or whether an error is worth retrying —
**Python decides all of that and says so in the response**.

The payoff: the queue is swappable. Set `QUEUE_DRIVER=arq` and a pure-Python queue
runs over the same Redis with zero changes to any stage.

\newpage

# Chapter 3 — Glossary (read this once, everything else gets easier)

| Term | Plain-English meaning |
| --- | --- |
| **Project** | A folder *and* a security wall. Contracts live inside a project. Your access is granted per project. |
| **Contract** | One uploaded document (PDF or DOCX), plus everything we learned from it. |
| **Job** | The processing run for one contract. It moves through 8 stages. |
| **Stage** | One step of processing (e.g. "Parser", "Chunking"). Each is independently retryable. |
| **Artifact** | The saved output of a stage — a JSON file in object storage. Doubles as a **checkpoint**. |
| **Checkpoint** | A saved stage result. If stage 6 fails, we resume from 6 — stages 1–5 are not redone. |
| **Parser** | The component that turns a PDF into text + layout + coordinates. |
| **CDM (Canonical Document Model)** | Our single, standard internal format for "a document". Everything after parsing reads only this — never raw parser output. |
| **DIP (Document Intelligence Profile)** | A configuration record that says *how* a given contract type is processed: which prompts, which mandatory clauses, how to chunk, how to score risk. Adding a new contract type = adding a profile row, **no code change**. |
| **Clause** | A single extracted contract term (e.g. "Limitation of Liability"), with its text, page, attributes and confidence. |
| **Chunk** | A meaningful passage of the document (a section, a clause, a table) used for search. Not a fixed-size slice. |
| **Embedding / vector** | A list of numbers that represents the *meaning* of text. Similar meanings = similar numbers. This is how "semantic search" works. |
| **Clause Master** | The admin-editable catalogue of clause types the system knows how to extract. |
| **Provenance** | The full paper trail on any extracted fact: document, page, bounding box, confidence, model version, prompt version. |
| **Copilot** | The chat assistant that answers questions using only the contracts you have access to. |
| **Citation** | A `[1]`-style marker in an answer that resolves to an exact page and highlight box. |
| **RAG** | "Retrieval-Augmented Generation" — find the relevant text first, then ask the AI to answer *only from that text*. |
| **Idempotent** | Running it twice produces the same result — no duplicated rows. Every stage is built this way. |

\newpage

# Chapter 4 — The two flows that matter

Everything in this backend is one of two journeys:

```
   JOURNEY A — "Getting knowledge IN"           JOURNEY B — "Getting answers OUT"

   User uploads a PDF                            User asks a question
          │                                              │
          ▼                                              ▼
   8-stage processing pipeline                    Plan → Retrieve → Assemble
          │                                              │      → Generate → Verify
          ▼                                              ▼
   Structured, searchable,                        A cited answer, with
   evidence-backed knowledge                      clickable page highlights
```

Chapter 5 covers Journey A. Chapter 6 covers Journey B.

\newpage

# Chapter 5 — Journey A: what happens when you upload a contract

## 5.1 The upload request itself (must finish in under 5 seconds)

When you drag 50 PDFs into the browser, the API does **only four things per file**
and then gets out of the way:

```
  Browser
    │  POST /api/v1/projects/{id}/contracts/upload   (multipart, up to 100 files)
    ▼
  ┌──────────────────────────────────────────────────────────────┐
  │  For EACH file:                                              │
  │                                                              │
  │  1. VALIDATE   Is the extension allowed (pdf/docx)?          │
  │                Is it under the size limit (default 200 MB)?  │
  │                Do the file's first bytes actually say "PDF"? │
  │                 └─ we do NOT trust the filename or the       │
  │                    browser's content-type; both are easy     │
  │                    to fake.                                  │
  │                                                              │
  │  2. FINGERPRINT  Compute a SHA-256 hash while streaming.     │
  │                  Already uploaded to this project?           │
  │                   → mark "duplicate", skip. No bytes stored. │
  │                                                              │
  │  3. STORE       Stream the file into object storage.         │
  │                 Re-verify the hash after writing.            │
  │                                                              │
  │  4. QUEUE       Create a Contract row, a Version row, and    │
  │                 a Job in state QUEUED. Put one message on    │
  │                 the queue.                                   │
  └──────────────────────────────────────────────────────────────┘
    │
    ▼
  Response:  { total: 50, accepted: 47, duplicates: 2, rejected: 1,
               files: [ ...per-file outcome... ], job_ids: [...] }
```

**Three deliberate behaviours worth knowing:**

- **Partial success is normal, not an error.** 2 duplicates in a 50-file upload must
  not fail the other 48. Every file gets its own result and its own reason.
- **Nothing slow happens in the request.** No parsing, no page counting, no virus
  scan. Those are *stages*, because a file can also be reprocessed months later
  through a completely different path.
- **If the queue is unreachable**, the affected jobs are immediately marked FAILED
  and the API says so. A contract silently stuck in QUEUED forever is worse than a
  visible failure.

## 5.2 The 8-stage pipeline

```
 QUEUED
   │
   ▼
 ┌─────────────────┐   artifact: validation.json
 │ 1. VALIDATION   │   Is this file real, safe, openable, non-empty?
 └────────┬────────┘
          ▼
 ┌─────────────────┐   artifact: normalized_document.json
 │ 2. PARSER       │   Turn the PDF into text + layout + coordinates.
 └────────┬────────┘
          ▼
 ┌─────────────────┐   artifact: canonical_document.json  ← the CDM
 │ 3. ENRICHMENT   │   Repair structure, reading order, cross-page clauses.
 └────────┬────────┘
          ▼
 ┌─────────────────┐   artifact: classification.json
 │ 4. CLASSIFICATION│  "This is an MSA" → selects the Document Profile
 └────────┬────────┘   which governs every stage after this one.
          ▼
 ┌─────────────────┐   artifact: chunks.json (+ stats, + validation)
 │ 5. CHUNKING     │   Cut the document into meaningful passages.
 └────────┬────────┘
          ▼
 ┌─────────────────┐   artifacts: clauses / entities / obligations /
 │ 6. AI EXTRACTION│              risks / timelines / relationships
 └────────┬────────┘   The actual "reading" of the contract.
          ▼
 ┌─────────────────┐   artifacts: summary / clause / chunk embeddings
 │ 7. EMBEDDING    │   Build the three-level "meaning index".
 └────────┬────────┘
          ▼
 ┌─────────────────┐   artifact: index_statistics.json
 │ 8. INDEXING     │   Verify searchability, build the knowledge graph.
 └────────┬────────┘
          ▼
       READY  ✔  (or NEEDS_REVIEW if the AI wasn't confident enough)

 Alternate states at any point:  FAILED · RETRYING · CANCELLED · PAUSED
```

### The four rules every stage obeys

1. **Retry only what failed.** If stage 6 fails, stages 1–5 keep their checkpoints.
   A retry starts at 6.
2. **Checkpoint after every success.** The artifact is written to storage and its
   pointer recorded in the database *before* the next stage is even queued.
3. **Idempotent.** Re-running a stage deletes its own previous output first, so you
   never end up with two copies of the same clause.
4. **Version-gated reuse.** Each checkpoint records the exact versions that produced
   it (parser version, prompt version, model, chunk strategy, profile version). If
   nothing relevant changed, the stage is *skipped* and reuses the checkpoint. This
   is what makes a re-run after a small config change nearly free.

### Stage by stage, in plain English

---

**Stage 1 — Validation** *(cheap, never skipped)*

Re-checks the file from *inside* the pipeline rather than trusting the upload
request. Checks the SHA-256 still matches (has the file been corrupted or swapped in
storage?), that it is not empty or oversized, that the magic bytes match the claimed
type, that a PDF is not password-protected or corrupt, that a DOCX actually contains
a `word/document.xml`, and — if enabled — runs a ClamAV malware scan.

*Key design choice:* rejecting a document is a **halt**, not a **failure**. A
password-protected PDF is a perfectly valid outcome of validation, not a system
error. So the stage succeeds, records *why* it stopped, and the user gets a sentence
they can act on ("The document is password protected and cannot be processed").

If malware scanning is enabled but the scanner is unreachable, the file is
**rejected**. A malware gate that fails open is not a gate.

---

**Stage 2 — Parser** *(the "read the pixels" stage)*

Converts the document into text, headings, paragraphs, tables, lists — and crucially
the **coordinates** of each of those on each page. Those coordinates are what later
lets the UI draw a yellow highlight box over the exact sentence an answer came from.

The stage itself contains **no parsing logic**. It picks an adapter from a registry:

| Parser | What it is |
| --- | --- |
| `idoc` *(default)* | Our in-house layout service (wraps Azure Document Intelligence). Returns real layout roles and coordinates. |
| `pymupdf` | Local, dependency-light fallback. Works offline. |
| `adi`, `textract`, `googledocai` | Reserved names for future adapters. Selecting one today gives a clear error naming the valid options, not a crash. |

Two more adapters exist but are not chosen via `ACTIVE_PARSER` — the registry picks
them automatically: a **DOCX adapter** (selected by file type, since Word files do
not go through a PDF layout service) and a **mock adapter** used by tests and CI.

Adapters can also run in **fixture mode** (the default) — replaying a recorded
response from disk instead of calling the live layout service. That is deliberate:
an accidental live call costs money and burns a rate limit, whereas an accidental
fixture replay is a loud, logged fallback.

Switching parsers is a config change (`ACTIVE_PARSER=...`) plus one adapter. Nothing
downstream changes, because every parser must emit the same **Normalized Document**
shape.

Two safety nets: a **timeout** (a hung parser would otherwise hold a worker slot
forever), and a **"did we actually get text?"** check — a parse that recovers almost
no text is failed deliberately rather than allowed to produce an empty contract.
OCR is applied per-page when a page turns out to be a scan.

---

**Stage 3 — Enrichment** *(the "make it make sense" stage)*

Takes the parser's output and builds the **Canonical Document Model** — one standard
internal format. This is where:

- global reading order is established (a two-column page must read correctly),
- a clause split across a page break is stitched back into one clause,
- broken section hierarchies are repaired,
- cross-references ("as defined in Section 4.2") are detected,
- honest quality metrics are computed (e.g. "87% of blocks have coordinates").

**From this stage onward, nothing reads parser output.** Chunking, extraction,
embedding, search and the Copilot all consume only the CDM. That single rule is what
makes the parser genuinely swappable.

If the CDM ends up with no body text at all, the pipeline halts here with a clear
message rather than pushing emptiness through four more stages.

---

**Stage 4 — Classification** *(the "what kind of contract is this?" stage)*

Decides the agreement type (MSA, NDA, Lease, Employment, SOW, …) and therefore
**which Document Intelligence Profile** processes it.

The classifier is **rule-scored, not hardcoded**. All its signals come from the
profiles stored in the database:

```
   Title patterns    (weight 0.45)  "MASTER SERVICES AGREEMENT" in the heading
   Required phrases  (weight 0.30)  vocabulary this type must contain
   Section headings  (weight 0.25)  structural signal that survives bad filenames
   Negative phrases                 vocabulary that rules a type OUT
                                    (an "NDA" mentioning "the Premises" is a lease)
        │
        ▼
   combined score ≥ 0.40 ?  ──no──►  optional LLM tie-break on the first 6,000 chars
        │yes                                    │
        ▼                                       ▼
   Profile selected  ◄───────────────────────────
```

The chosen profile is **pinned onto both the job and the contract**. If someone edits
that profile next month, this contract still records the exact version that
interpreted it. Nothing is silently rewritten.

Low confidence is **propagated, not smoothed over** — a contract classified below its
profile's threshold is flagged `needs_review`, because that is precisely the case a
human should look at.

---

**Stage 5 — Chunking** *(cutting the document into searchable pieces)*

Not fixed-size slices. Chunks follow the document's own structure:

`section` · `clause` · `paragraph` · `table` · `list` · `definition` · `appendix` ·
`signature` · `footnote`

The **strategy comes from the profile**, not from code — so a lease is chunked
table-first and an NDA clause-first with no `if` statement anywhere:

| Strategy | Use |
| --- | --- |
| `hybrid` *(default)* | Best general-purpose mix |
| `section_based` | Follows the numbered section tree |
| `heading_aware` | Uses headings as boundaries |
| `clause_based` | One chunk per clause |
| `table_preserving` | Never splits a table across chunks |
| `list_preserving` | Never splits a list |

Chunk size, minimum size and overlap all come from the profile too
(`max_tokens`, `min_tokens`, `overlap_tokens`).

*Idempotency detail:* re-chunking deletes the old chunks **and** the chunk-level
vectors that pointed at them — because a vector pointing at a deleted chunk is a
search result with no evidence behind it. It deliberately leaves the document- and
clause-level vectors alone, so a re-chunk does not force a needless re-embed.

---

**Stage 6 — AI Extraction** *(the stage users actually came for)*

This is where the system reads the contract and produces:

- **Clauses** — with typed attributes (e.g. liability cap basis, carve-outs,
  notice period, governing law), the exact quoted text, page and confidence.
- **Entities / Parties** — who the parties are, and which side is *us* (matched
  against configured organisation names, so "can *we* terminate for convenience?"
  is answerable without hardcoding a company name anywhere).
- **Obligations** — who must do what, by when.
- **Key dates** — effective, expiry, renewal, notice deadlines, milestones.
- **Risks** — plus a **0–100 risk score**.
- **Relationships** — references, amendments, dependencies between documents.
- **Missing mandatory clauses** — what this contract type *should* have and does not.

Five design decisions that matter here:

1. **One AI call per clause category, not one giant call.** Each category gets its
   own strict output schema and its own pre-filtered evidence. One combined call
   would need a weak union schema, would send the whole document (expensive), and
   would lose *everything* when it failed.
2. **Priority order is real.** Categories are extracted in Clause Master priority
   order. Limitation of Liability is first, because that is what people open a
   contract to check. A run cut short has still produced what matters most.
3. **A failing category never fails the job.** 22 clauses out of 23 is a useful
   contract; a failed job is not. Which categories failed is recorded, surfaced as a
   warning, and flags the contract for review.
4. **No candidate evidence → no AI call.** If the deterministic pre-filter finds
   nothing that looks like a liability clause, we report "not found" and spend zero
   tokens. This is not just a saving — a model handed unrelated text and asked for a
   liability cap will sometimes invent one.
5. **The engine never touches the database.** It takes chunks in and returns results
   out. The stage handler does the writing. That is why the extraction logic can be
   tested against a fixture with no database and no API key.

### How the 0–100 risk score works

The risk score is **not** an AI output. It is computed by named, deterministic rules,
so a reviewer can decompose "78 — High" into the individual findings that produced it
and argue with any one of them.

```
  severity weights:   critical = 40   high = 25   medium = 10   low = 3
  bands:              0–33 Low        34–66 Medium        67–100 High
  cap:                no single finding may contribute more than 40 points
                      (so one issue can't saturate the scale and hide everything else)
  weights per risk type come from the profile — an insurance policy and an NDA
  can weight "auto-renewal" completely differently, with no code change.
```

Two findings are treated as first-class because they are what the whole clause list
was prioritised around:

- **Effective unlimited liability.** A cap of "2× fees paid" *with carve-outs* for IP
  infringement, confidentiality breach and gross negligence is **not** a limited-
  liability contract. Carve-outs are scored separately from the cap so a tidy-looking
  cap value cannot hide them.
- **Missing mandatory clauses.** An absent limitation of liability is a bigger finding
  than a bad one. Because absence has no text to quote, these are marked as
  *omissions* and exempted from the evidence checks that assume there is something
  to cite.

---

**Stage 7 — Embedding** *(building the "meaning index")*

Text is converted into vectors — long lists of numbers where similar meanings sit
close together. We build **three levels**, deliberately:

```
  L1  DOCUMENT SUMMARY   one vector per contract
        └─ "which documents are even relevant?"  → narrows thousands to dozens

  L2  CLAUSE             one vector per extracted clause
        └─ "find me every indemnity clause like this one"

  L3  CHUNK              one vector per chunk
        └─ "find the exact passage that answers this question"
```

**Selective regeneration is the whole point.** Before generating anything, the stage
asks the database: *"which of these exact texts do you already have a vector for,
under the current version set?"* Unchanged text reuses its vector and costs nothing.
That is what makes a re-run after a prompt-only change nearly free, and stops a
re-parse of a 300-page agreement from re-billing every chunk.

Reuse requires the content hash **and every version** to match. Reusing a vector
across a model change would silently mix two different vector spaces in one index,
and distances between them are meaningless.

### The `halfvec` decision (the one non-obvious thing in the vector store)

Our default embedding model emits **2048 dimensions**. pgvector's HNSW index supports
at most **2000** for the `vector` type.

A `vector(2048)` column is accepted by Postgres, accepts inserts happily, and then
**cannot carry an index at all** — every similarity search silently degrades to
scanning the whole table. Nothing errors. The system just gets slower and slower as
the corpus grows.

So we store as **`halfvec`**, which indexes up to 4000 dimensions and uses half
precision. The column is actually *smaller* than the 1536-dimension `vector` it
replaces (4096 bytes vs 6144), and for normalised embeddings the precision loss is
far below the margin that separates a relevant hit from an irrelevant one.

**The API refuses to start** if it is configured with `vector` storage and more than
2000 dimensions — because that failure is otherwise completely invisible.

---

**Stage 8 — Indexing** *(making it all reachable)*

The final stage. It does three things, none of which call an AI model:

1. **Verifies keyword-search coverage.** The keyword index (`search_vector`) is
   maintained by a database trigger, so this stage *checks* it rather than computing
   it. A chunk with no keyword vector is invisible to half of hybrid search — finding
   that out here beats finding it out from a user's failed search.
2. **Builds the knowledge graph.** Extraction recorded what the text *says* ("see
   Schedule B"); this resolves those references into real edges between real rows,
   and records the ones that resolve to nothing.
3. **Marks the contract READY.** Only once its vectors and graph exist. Until then
   it is *processing*, not *searchable-but-incomplete*.

## 5.3 What happens when a stage fails

```
   Stage handler raises
          │
          ▼
   ┌──────────────────────────────────────────────────────────┐
   │ Is this error retryable?                                 │
   │  (the error itself says; e.g. a provider rate limit yes, │
   │   a corrupt file no)                                     │
   └───────────┬──────────────────────────┬───────────────────┘
               │ YES, attempts left       │ NO, or attempts exhausted
               ▼                          ▼
   ┌───────────────────────┐   ┌──────────────────────────────────┐
   │ state = RETRYING      │   │ state = FAILED                   │
   │ re-queue SAME stage   │   │ contract status = FAILED         │
   │ exponential backoff:  │   │ operational ALERT raised         │
   │   delay × 2^(n-1)     │   │ job carries a human-readable     │
   │ completed stages keep │   │ reason and the failing stage     │
   │ their checkpoints     │   │                                  │
   └───────────────────────┘   └──────────────────────────────────┘
```

Two extra safeguards:

- **Unexpected exceptions never leak internals.** If something throws that we did not
  anticipate, the job records `"An unexpected error occurred."` — the full traceback
  goes to the operator's logs, not to the user's screen (a raw exception message can
  contain a database connection string or a file path).
- **The next stage is queued only after the transaction commits.** Otherwise a fast
  worker could start stage 7 before stage 6's checkpoint was actually durable.

## 5.4 Reprocessing

```bash
make reprocess job=<job-uuid> stage=embedding
```

Re-runs that stage and everything after it. The Workflow Engine refuses a resume
whose earlier stages never succeeded ("cannot resume at embedding: chunking has not
completed") — otherwise you would get a confusing failure deep in the pipeline
against artifacts that do not exist.

\newpage

# Chapter 6 — Journey B: how a question gets answered

## 6.1 The full answer path

```
  "Which of our vendor contracts have uncapped liability and expire this year?"
        │
        ▼
 ┌────────────────────────────────────────────────────────────────────────┐
 │ 1. AUTHENTICATE + SCOPE                                                │
 │    Who is asking? Which projects are they a member of?                 │
 │    → an EMPTY scope returns EMPTY results, never "everything".         │
 └───────────────────────────────┬────────────────────────────────────────┘
                                 ▼
 ┌────────────────────────────────────────────────────────────────────────┐
 │ 2. RETRIEVAL PLANNER  — decides WHAT to fetch and HOW. Executes nothing.│
 │                                                                        │
 │    Detects intent:  metadata_lookup · clause_lookup · obligation ·     │
 │                     risk_assessment · comparison · summarization ·     │
 │                     timeline · relationship · compliance · financial   │
 │                                                                        │
 │    Picks a strategy: metadata_only · metadata_plus_document ·          │
 │                      clause_retrieval · chunk_retrieval ·              │
 │                      graph_traversal · hybrid (default)                │
 │                                                                        │
 │    Extracts filters the question implies (dates, vendor, type, risk).  │
 │                                                                        │
 │    Rule-based by default — free, instant, reproducible. An LLM pass    │
 │    runs only for genuinely ambiguous questions.                        │
 └───────────────────────────────┬────────────────────────────────────────┘
                                 ▼
 ┌────────────────────────────────────────────────────────────────────────┐
 │ 3. RETRIEVAL ENGINE — executes the plan. Makes no decisions.           │
 │                                                                        │
 │    a) METADATA PRE-FILTER   indexed lookup, runs FIRST, shrinks        │
 │                             everything after it. Capped at 200         │
 │                             candidate contracts.                       │
 │    b) L1 document vectors   rank candidate DOCUMENTS                   │
 │    c) L2 clause + L3 chunk  search only inside the survivors           │
 │    d) KEYWORD search        Postgres full-text (ts_rank), in parallel  │
 │    e) FUSION                Reciprocal Rank Fusion (k=60) merges the   │
 │                             vector and keyword rankings                │
 │    f) RE-RANK               optional cross-encoder pass                │
 │    g) EXPANSION             pull in neighbouring and parent chunks so  │
 │                             a clause arrives with its context          │
 └───────────────────────────────┬────────────────────────────────────────┘
                                 ▼
 ┌────────────────────────────────────────────────────────────────────────┐
 │ 4. CONTEXT ASSEMBLY — what actually FITS, in what order                │
 │                                                                        │
 │    • Budget: evidence may use ~55% of the model's input window.        │
 │    • No single passage may exceed 25% of the budget (or one huge       │
 │      chunk crowds out every other citation).                           │
 │    • Minimum 3 items admitted regardless, so a tight budget still      │
 │      produces a citable answer.                                        │
 │    • Each admitted item gets a stable label: [1], [2], [3]…            │
 │    • Grouped by contract, so a multi-contract answer is readable.      │
 │    • Anything DROPPED is recorded — a silently truncated context       │
 │      produces an answer that looks complete and is not.                │
 └───────────────────────────────┬────────────────────────────────────────┘
                                 ▼
 ┌────────────────────────────────────────────────────────────────────────┐
 │ 5. PROMPT ORCHESTRATOR — builds the instruction                        │
 │    Chooses an output shape: natural language · executive summary ·     │
 │    risk report · compliance report · clause comparison · timeline ·    │
 │    action items · JSON …                                               │
 └───────────────────────────────┬────────────────────────────────────────┘
                                 ▼
 ┌────────────────────────────────────────────────────────────────────────┐
 │ 6. RAG ENGINE — generates the answer.                                  │
 │    It NEVER retrieves and NEVER builds prompts. It only consumes the   │
 │    Context Package. If a fact isn't in the package, it cannot be used. │
 └───────────────────────────────┬────────────────────────────────────────┘
                                 ▼
 ┌────────────────────────────────────────────────────────────────────────┐
 │ 7. RESPONSE VALIDATOR — the part that keeps Promise #1                 │
 │                                                                        │
 │  ✗ FABRICATED CITATIONS ARE STRIPPED. If the model writes "[7]" but    │
 │    only [1]–[5] were offered, [7] is removed from the text. A bracketed│
 │    number the UI cannot resolve reads as verifiable and is not.        │
 │  ⚑ UNCITED SUBSTANTIVE ANSWERS ARE FLAGGED. A long answer stating      │
 │    contract terms with zero citations did not come from the evidence.  │
 │  ✓ HONEST NON-ANSWERS ARE REWARDED. "The contract does not address     │
 │    this" is recognised and scored ~0.75, not punished for having no    │
 │    citations.                                                          │
 │  # CONFIDENCE MEASURES GROUNDING, NOT FLUENCY:                         │
 │       0.55 × citation coverage  +  0.45 × evidence strength            │
 │       × 0.5 if any citation was fabricated                             │
 │       × 0.9 if evidence was dropped for budget                         │
 └───────────────────────────────┬────────────────────────────────────────┘
                                 ▼
        Answer + citations → each citation carries document, page,
        bounding box, confidence and artifact version → the PDF viewer
        draws the highlight over the exact source text.
```

## 6.2 Why "metadata first" is a rule, not an optimisation

*"Which contracts expire next quarter?"* is a **date range query over a table**. It is
not a similarity search. Answering it with a vector scan is both slower **and wrong**
— similarity has no opinion about dates.

So the planner decides this once, centrally, instead of every endpoint re-litigating
it. And because the metadata filter runs before the vector search, a repository of
millions of contracts still answers fast.

## 6.3 Refusals are a first-class outcome

Contract language around indemnities, breach and liability sits close enough to AI
safety categories that false-positive refusals are a real operational risk. So when a
model declines, the system reports it honestly and distinguishably:

> *"The model declined to answer this question. This can happen when contract
> language resembles a restricted topic. Rephrasing the question, or narrowing it to
> a specific clause, usually resolves it."*

The caller can tell "the model declined" apart from "the model failed" — which
matters for both the user experience and the metrics.

## 6.4 Copilot sessions

The Copilot keeps chat sessions with history. The last **6 turns** are fed back into
the prompt — enough for pronouns ("what about *its* renewal terms?") to resolve,
without spending the evidence budget on conversation transcript. Answers can also be
**streamed** token-by-token; citation validation then runs once the stream completes,
because a citation is only checkable when the text containing it exists.

\newpage

# Chapter 7 — Security: who can see and do what

## 7.1 The three-layer check

Every protected request passes three gates, each depending on the previous one:

```
   ┌───────────────────────────────────────────────────────────────┐
   │ GATE 1 — AUTHENTICATION:  "Who are you?"                      │
   │   Bearer JWT → decode → LOAD THE USER ROW FROM THE DATABASE   │
   │                          on every single request.             │
   │   Why: a token issued before an account was deactivated must  │
   │   stop working immediately, not at expiry.                    │
   └───────────────────────────┬───────────────────────────────────┘
                               ▼
   ┌───────────────────────────────────────────────────────────────┐
   │ GATE 2 — PROJECT ACCESS:  "May you see this project at all?"  │
   │   No membership row → 404 NOT FOUND (deliberately, not 403).  │
   │   Why: confirming that a project id exists is itself a leak   │
   │   across the isolation boundary. An outsider must not be able │
   │   to probe for valid ids by comparing 403s against 404s.      │
   └───────────────────────────┬───────────────────────────────────┘
                               ▼
   ┌───────────────────────────────────────────────────────────────┐
   │ GATE 3 — PERMISSION:  "May you do THIS specific thing?"       │
   │   e.g. contract:upload, knowledge:review, export:create       │
   │   Permissions are read from the DATABASE per request, never   │
   │   from the token — so revoking access takes effect instantly. │
   └───────────────────────────────────────────────────────────────┘
```

Endpoints declare their requirement in the function signature, so the check is
impossible to forget and shows up in the API docs automatically.

## 7.2 The four roles

| Role | UI label | What it means |
| --- | --- | --- |
| `system_admin` | Admin | Platform administration. The only role that crosses project boundaries. |
| `project_manager` | Contract Manager | Runs a project: uploads, manages members, reviews. |
| `reviewer` | Reviewer | Reviews and corrects AI extractions. |
| `viewer` | Viewer | Read-only. |

Single sign-on users are **not** a separate role — an SSO-authenticated person is
granted one of these four per project when invited.

## 7.3 The separation-of-duties rule (important and easy to miss)

**A System Administrator cannot upload contracts.** Deliberately.

The administrator is an oversight and governance role: it reads every project,
creates projects, provisions users. Someone who can both grant themselves access to
any project **and** put documents into it has no separation of duties left, and the
audit trail stops being able to answer *"who brought this contract into the
repository?"*

This is enforced structurally: admins receive "every permission **minus** the
excluded set", built by *subtraction*. So a permission added to the system later is
granted to admins automatically, and one that must stay out of admin hands has to be
named explicitly. An admin who is also a project member still cannot pick the
capability back up through that membership — otherwise the restriction would be one
"add me to the project" away from being undone by the person it constrains.

The error message is specific rather than generic, because "your role does not grant
this" is actively confusing for an account that holds every other permission on the
platform.

## 7.4 The full permission list

| Area | Permissions |
| --- | --- |
| Projects | `project:create` `project:read` `project:update` `project:delete` `project:member:manage` |
| Contracts | `contract:upload` `contract:read` `contract:update` `contract:delete` `contract:download` |
| Processing | `job:read` `job:control` (retry / cancel / pause / resume) |
| Knowledge | `knowledge:read` `knowledge:review` (accept/reject an AI extraction) |
| Retrieval | `search:execute` `copilot:use` |
| Reporting | `report:view` `export:create` |
| Administration | `user:manage` `role:manage` `clause_master:manage` `ai_settings:manage` `profile:manage` `audit:read` `alert_rule:manage` |

Per-member **overrides** can *narrow* a role for one person. They can never widen it.

## 7.5 Other security measures

| Measure | Detail |
| --- | --- |
| Password hashing | Argon2 (default) or bcrypt. Never reversible. |
| Access tokens | JWT, 30 minutes default. |
| Refresh tokens | 14 days, stored in an **HttpOnly cookie** — JavaScript cannot read it, so an XSS bug cannot steal the session. |
| Forced password change | A seeded or newly-provisioned account is blocked from all normal API use until the default password is changed. |
| Rate limiting | 120 requests/minute default; 10/minute on login. |
| Internal endpoints | `/internal/*` is guarded by a shared secret compared in **constant time**, hidden from the public API schema, and in Kubernetes unreachable from outside the namespace. |
| Upload safety | Magic-byte sniffing, size limits enforced while streaming, optional ClamAV scan. |
| Storage keys | Every key is project-prefixed and filename-sanitised, so a crafted filename cannot escape its project's prefix. |
| Production guards | The app **refuses to start** in production with a default JWT secret, a default admin password, a default internal token, or mock AI providers (unless `ALLOW_MOCK_AI=true` records the decision deliberately). |
| Audit log | Every meaningful action is recorded: login, upload, download, export, search, Copilot query, job control, review decisions, permission changes, config changes. |

\newpage

# Chapter 8 — What's in the database

36 tables. Here they are grouped by what they are for.

> For the **complete** schema — every column, type, constraint, index, enum and
> trigger — see the companion document
> [`DATABASE_SCHEMA.md`](DATABASE_SCHEMA.md). This chapter is the orientation
> version.

## 8.1 Identity and access

| Table | Holds |
| --- | --- |
| `users` | People. Email, name, hashed password, active flag, SSO provider. |
| `roles` | The four roles, each with a JSON list of permissions. |
| `refresh_tokens` | Active sessions, so they can be listed and revoked. |
| `projects` | The security boundary. Settings, contract counts, status. |
| `project_members` | Who is in which project, with which role and overrides. |
| `project_activities` | Human-readable activity feed per project. |

## 8.2 Documents and processing

| Table | Holds |
| --- | --- |
| `contracts` | One row per uploaded document: file info, hash, status, agreement type, page count, profile version, review flag. |
| `contract_versions` | Every uploaded version of a contract. Old bytes are never overwritten — earlier extractions reference them. |
| `contract_metadata` | The flattened "projection" the dashboards, filters and metadata pre-filter read: parties, dates, risk band, governing law, missing clauses, unlimited-liability flag. |
| `processing_jobs` | One row per processing run: state, progress %, timings, execution plan, error, accumulated metrics. |
| `job_stage_runs` | One row per stage attempt: status, duration, worker id, versions, artifact pointer, error. **These are the checkpoints.** |
| `document_artifacts` | Pointers to the JSON artifacts in object storage: path, checksum, size, generation, versions. Large payloads never enter Postgres. |
| `document_profiles` | The DIPs. Versioned. |

## 8.3 Extracted knowledge

| Table | Holds |
| --- | --- |
| `clauses` | Extracted clauses with typed attributes, quoted text, page, bounding boxes, confidence, review status. |
| `entities` | Parties, organisations, people, signatories — with which side of the deal they are on. |
| `obligations` | Who must do what, by when, current status. |
| `risks` | Individual risk findings with severity and recommendation. |
| `key_dates` | Effective, expiry, renewal, notice, milestone, payment dates. |
| `knowledge_relationships` | Edges: references, amendments, dependencies. |
| `contract_summaries` | Generated executive summaries. |
| `chunks` | The searchable passages, with a Postgres full-text `search_vector` maintained by a trigger. |
| `embeddings` | The vectors (`halfvec`), one row per item per level, with content hash + all version columns for reuse. |
| `graph_nodes` / `graph_edges` | The knowledge graph. |

Every one of these tables carries `project_id` and is filtered by it on every read.

## 8.4 Governance, operations and history

| Table | Holds |
| --- | --- |
| `clause_master_categories` | The admin-editable catalogue of clause types. |
| `clause_master_rules` | Versioned extraction rules + output schemas per category. |
| `ai_settings` | Runtime AI configuration. |
| `alerts` / `alert_rules` | Operational and business alerts, and the rules that raise them. |
| `export_jobs` | Export requests and their generated files. |
| `chat_sessions` / `chat_messages` | Copilot conversation history. |
| `audit_log` | Who did what, when, from where. |
| `contract_history` / `clause_history` | Field-level change history. |
| `retrieval_audit` | What was searched, what plan ran, what was returned. |

## 8.5 How the main tables relate

```
   users ──────┬──── project_members ────┬────── projects
               │                          │
               │                          ├────── project_activities
               │                          │
               │                          └────── contracts ───┬─ contract_versions
               │                                       │       ├─ contract_metadata
               └──── audit_log                         │       │
                                                       │       │
        ┌──────────────────────────────────────────────┘       │
        │                                                      │
        ├── processing_jobs ── job_stage_runs                  │
        │                   └─ document_artifacts              │
        │                                                      │
        ├── chunks ──────────────────┐                         │
        ├── clauses ─────────────────┤                         │
        ├── entities                 ├── embeddings  ◄─────────┘
        ├── obligations              │   (one row per item,
        ├── risks                    │    per level, with the
        ├── key_dates                │    content hash used for
        ├── knowledge_relationships  │    free reuse)
        └── contract_summaries       │
                                     │
        graph_nodes ── graph_edges ◄─┘
```

\newpage

# Chapter 9 — The API surface

Base path: `/api/v1`. Interactive docs at `/docs` (Swagger) and `/redoc`.

## 9.1 Authentication

| Method | Path | What it does |
| --- | --- | --- |
| GET | `/auth/methods` | Which sign-in methods are enabled (password, Microsoft SSO). |
| POST | `/auth/login` | Sign in. Returns an access token + sets the refresh cookie. |
| POST | `/auth/refresh` | Get a new access token from the refresh cookie. |
| POST | `/auth/logout` | Sign out and revoke the session. |
| GET | `/auth/me` | The current user, roles and permissions. |
| GET | `/auth/sessions` | List your active sessions. |
| POST | `/auth/change-password` | Change your password. |
| GET | `/auth/oidc/authorize` | Start "Sign in with Microsoft". |
| GET | `/auth/oidc/callback` | Finish it. |

## 9.2 Users and roles

| Method | Path | What it does |
| --- | --- | --- |
| GET / PATCH | `/profile` | Your own profile. |
| GET | `/roles` | List roles and their permissions. |
| PATCH | `/roles/{role_name}` | Edit a role's permissions. |
| GET | `/users` | List users (admin). |
| GET | `/users/assignable` | Users who can be added to a project. |
| POST | `/users` | Create a user. |
| GET / PATCH / DELETE | `/users/{user_id}` | Manage one user. |
| POST | `/users/{user_id}/password` | Set a user's password (forces a change at next sign-in). |

## 9.3 Projects

| Method | Path | What it does |
| --- | --- | --- |
| GET / POST | `/projects` | List / create projects. |
| GET / PATCH / DELETE | `/projects/{id}` | Manage a project. |
| PUT | `/projects/{id}/favourite` | Star it. |
| GET / POST | `/projects/{id}/members` | List / add members. |
| POST | `/projects/{id}/members/bulk` | Add several at once. |
| PATCH / DELETE | `/projects/{id}/members/{user_id}` | Change a role / remove a member. |
| GET | `/projects/{id}/activities` | Project activity feed. |
| GET | `/activities` | Activity across all your projects. |

## 9.4 Contracts

| Method | Path | What it does |
| --- | --- | --- |
| POST | `/projects/{id}/contracts/upload` | Upload up to 100 files. |
| GET | `/projects/{id}/contracts` | List a project's contracts (filtered, paginated). |
| GET | `/contracts` | List across all your projects. |
| GET / PATCH / DELETE | `/contracts/{id}` | Manage one contract. |
| PATCH | `/contracts/{id}/metadata` | Correct extracted metadata by hand. |
| GET | `/contracts/{id}/versions` | Version history. |
| GET | `/contracts/{id}/file` | Get a short-lived signed URL to the original file. |
| GET | `/contracts/{id}/content` | Stream the original file through the API. |

## 9.5 Processing

| Method | Path | What it does |
| --- | --- | --- |
| GET | `/contracts/{id}/jobs` | This contract's processing runs. |
| POST | `/contracts/{id}/jobs/reprocess` | Re-run from a chosen stage. |
| GET | `/jobs` | The processing queue view. |
| GET | `/jobs/{id}` | Job detail: state, progress, timings, errors. |
| GET | `/jobs/{id}/stages` | Per-stage timeline for that job. |
| POST | `/jobs/{id}/retry` | Retry a failed job. |
| POST | `/jobs/{id}/cancel` | Cancel it. |
| GET | `/jobs/-/health` | Pipeline health: queue depths, stalled jobs, dead letters. |

## 9.6 Knowledge (what we extracted)

| Method | Path | What it does |
| --- | --- | --- |
| GET | `/contracts/{id}/knowledge` | Everything, arranged into the UI's tabs. |
| GET | `/contracts/{id}/clauses` | Extracted clauses. |
| GET | `/contracts/{id}/parties` | Contracting parties. |
| GET | `/contracts/{id}/obligations` | Obligations. |
| GET | `/contracts/{id}/risks` | Risk assessment + score. |
| GET | `/contracts/{id}/dates` | Key dates. |
| GET | `/contracts/{id}/evidence/{chunk_id}` | The source passage behind a citation, with coordinates. |
| GET | `/contracts/{id}/clauses/{clause_id}` | One clause in full. |
| POST | `/contracts/{id}/clauses/{clause_id}/review` | Approve / reject / correct an AI extraction. |

## 9.7 Search and Copilot

| Method | Path | What it does |
| --- | --- | --- |
| POST / GET | `/search` | Hybrid search. Returns hits **plus an explanation of the plan** that ran. |
| POST | `/copilot/ask` | Ask a question, get a cited answer. |
| POST | `/copilot/stream` | Same, streamed token by token. |
| POST / GET | `/copilot/sessions` | Create / list chat sessions. |
| GET / DELETE | `/copilot/sessions/{id}` | Read / delete one session. |

## 9.8 Dashboards, alerts, exports, admin

| Method | Path | What it does |
| --- | --- | --- |
| GET | `/dashboard` | KPIs: contract counts, risk distribution, expiring soon, top risks, upload trend. |
| GET | `/dashboard/processing` | Pipeline throughput and stage statistics. |
| GET | `/alerts` | List alerts. |
| PATCH | `/alerts/{id}` | Acknowledge / resolve / dismiss. |
| GET / POST | `/alerts/rules` | Manage alert rules. |
| PATCH / DELETE | `/alerts/rules/{id}` | Edit / remove a rule. |
| GET | `/exports/capabilities` | Which formats this deployment can produce. |
| POST | `/exports` | Start an export job. |
| POST | `/projects/{id}/exports` | Start a project-scoped export. |
| GET | `/exports` | Your export jobs. |
| GET | `/exports/{id}` | Export status. |
| GET | `/exports/{id}/download` | Short-lived signed download URL (audited every time). |
| GET / POST | `/clause-master` | The clause catalogue. |
| GET / PATCH | `/clause-master/{id}` | One clause category. |
| GET / POST | `/clause-master/{id}/rules` | Versioned extraction rules for it. |

## 9.9 Operational endpoints (not part of the public product API)

| Method | Path | What it does |
| --- | --- | --- |
| GET | `/healthz` | Liveness — "is the process alive?" |
| GET | `/readyz` | Readiness — "are the database, storage, queue and AI providers reachable?" |
| GET | `/metrics` | Prometheus metrics. |
| GET | `/version` | Component versions. |
| POST | `/internal/stages/{stage}/run` | **Internal only.** Run one pipeline stage. Shared-secret guarded. |
| GET | `/internal/stages` | Which stages this worker serves. |
| GET | `/internal/health` | Worker readiness. |

\newpage

# Chapter 10 — The queue, in detail

## 10.1 The message

The queue message contains **identifiers only** — never document content:

```json
{
  "job_id": "…", "contract_id": "…", "project_id": "…",
  "stage": "chunking",
  "attempt": 1,
  "priority": "normal",
  "trace": { "traceparent": "00-…" },
  "continue_pipeline": true,
  "options": {}
}
```

Three reasons this matters: messages stay small; they are **safe to log** (no
contract text ever lands in a log line); and a re-delivered message always reads
*current* state from the database rather than acting on a stale snapshot.

## 10.2 The dispatch sequence

```
  Python (API or runner)                Node (BullMQ)                Python (worker)
         │                                    │                             │
         │  POST /enqueue  ──────────────────►│                             │
         │                                    │ push to queue               │
         │                                    │ "cip:chunking"              │
         │                                    │                             │
         │                                    │ worker picks it up          │
         │                                    │                             │
         │                                    │  POST /internal/stages/     │
         │                                    │       chunking/run ────────►│
         │                                    │                             │ runs the
         │                                    │                             │ stage,
         │                                    │                             │ writes the
         │                                    │                             │ checkpoint
         │                                    │◄──── 200 OK ────────────────│
         │                                    │  { status, next_stage,      │
         │                                    │    should_retry,            │
         │                                    │    retry_delay_ms }         │
         │                                    │                             │
         │                                    │ marks the BullMQ job        │
         │                                    │ done / retried / dead       │
         │                                    │                             │
         │◄─── the PYTHON runner enqueues the next stage itself ────────────│
         │      (after its transaction commits — never before)              │
```

**A stage failure is a `200 OK` with `status: "failed"`, not an HTTP error.** The
dispatcher must be able to tell "the stage ran and failed — Python already recorded
it and already decided about retrying" apart from "the call never got through, so
nothing was recorded". Conflating those two would either double-run stages or
silently drop them.

## 10.3 Three interchangeable queue drivers

| Driver | When | How |
| --- | --- | --- |
| `bullmq` *(default)* | Production | Python POSTs to the Node dispatcher; Node workers call back. |
| `arq` / Redis list | Node-free deployments | Pure Python over the same Redis, with a sorted-set delay tier. |
| `inline` | Tests only | Runs the stage immediately, in-process. Makes the whole pipeline synchronous so a test can upload a document and assert on the finished result. |

## 10.4 Worker pools

| Pool | Serves | Why separate |
| --- | --- | --- |
| `worker-parser` | Validation, Parser, Enrichment, Classification, Chunking | CPU- and IO-heavy. |
| `worker-ai` | AI Extraction, Embedding, Indexing | Latency-bound on an external AI provider. Scales on a different curve. |
| `all` | Everything | The single-container path. |

Scale one independently:

```bash
podman compose up -d --scale worker-ai=4
```

## 10.5 The scheduler

A separate small service (`python -m app.cli scheduler`) sweeps every 60 seconds. It
exists because several things can be silently left behind:

| Sweep | What it fixes |
| --- | --- |
| **Stalled-job reclamation** | A crashed worker leaves a job "running" forever and the contract stuck in PARSING. The heartbeat is what distinguishes that from a genuinely slow parse (default threshold: 45 minutes). Such jobs are marked **FAILED, not requeued** — the stage may have been part-way through writing rows, so a blind re-run could duplicate work. A failed job is visible and can be reprocessed deliberately. |
| **Stalled-export recovery** | An export runs as a background task that dies with its process; the database row survives. A row stuck in "running" is a progress bar the user watches forever. |
| **Expired-export purge** | Deletes export files past their retention window, so an export does not become a permanent second copy of contract data. |

Time-based alerts (renewal and expiry deadlines) are evaluated on the same schedule —
something has to notice a deadline passing.

## 10.6 Dead letters

A job that exhausts its retries is moved to a **dead-letter queue** rather than left
in BullMQ's own failed list — because retention would eventually trim that list, and
a job that needs a human decision must not disappear on a timer.

\newpage

# Chapter 11 — Files, artifacts and storage

## 11.1 What goes where

| Data | Lives in | Why |
| --- | --- | --- |
| Original PDF/DOCX | Object storage | Big binary. Databases are bad at this. |
| Stage artifacts (JSON) | Object storage | Can be tens of MB for a large contract. |
| Generated exports | Object storage | Same. |
| Pointers, checksums, versions, summaries | PostgreSQL | Small, indexed, queryable. An incremental check is one indexed lookup, not a storage read. |
| Structured facts (clauses, dates, risks) | PostgreSQL | Needs filtering, joining and aggregating. |
| Vectors | PostgreSQL (pgvector) | Needs to be searched alongside the metadata filters. |

## 11.2 The key layout

Every key is **project-prefixed**, so a stray key can never land outside its
project's area:

```
  projects/{project_id}/contracts/{contract_id}/v{version}/{filename}
  projects/{project_id}/contracts/{contract_id}/artifacts/{kind}/g{generation}.json
  projects/{project_id}/contracts/{contract_id}/pages/{page}.png
  projects/{project_id}/exports/{export_id}/{filename}
```

Filenames are sanitised (path traversal via a crafted filename is the obvious risk;
a leading dot or a backslash on a Windows host is the less obvious one).

## 11.3 Artifact generations

Artifacts are never overwritten. Re-running a stage writes generation `g2`, `g3`… and
the previous generation is **superseded**, not deleted. This preserves the provenance
chain: an extraction from three months ago still points at the exact artifact that
produced it.

## 11.4 Signed URLs, not proxying

The browser fetches PDFs **directly from storage** using a short-lived signed URL
(default 15 minutes). Streaming every page render through the API would make the API
the bottleneck for the document viewer.

> **Deployment gotcha worth knowing:** `S3_ENDPOINT_URL` is where the *application*
> reaches storage (often an internal hostname like `http://minio:9000`).
> `S3_PUBLIC_ENDPOINT_URL` is where the *browser* reaches it. If you leave the second
> unset behind a private network, the API happily signs a URL the browser cannot
> resolve — every server-side check reports healthy and the PDF viewer just shows
> nothing. The signature is computed against the public host directly, because
> patching a hostname into an already-signed URL produces a 403.

\newpage

# Chapter 12 — AI providers and swappability

## 12.1 Two independent choices

| | Purpose | Options |
| --- | --- | --- |
| **`LLM_PROVIDER`** | Reading the contract, answering questions | `gemini` · `anthropic` · `openai` · `azure_openai` · `local` · `mock` |
| **`EMBEDDING_PROVIDER`** | Building the meaning index | `nvidia` · `openai` · `azure_openai` · `sentence_transformers` · `mock` |

Adapters are imported **lazily**, so a deployment installs only the SDK it actually
uses. Choosing a provider whose package is missing gives an actionable message
(`Anthropic support requires the 'ai' extra: pip install '.[ai]'`), not an import
traceback.

## 12.2 Running with zero AI credentials

`LLM_PROVIDER=mock` and `EMBEDDING_PROVIDER=mock` are the **defaults**. The entire
pipeline runs end to end with no vendor account and no API key — upload, parse,
classify, chunk, extract, embed, index, search, Copilot and export.

**Be clear about what that gives you.** The mock provider is deterministic and
schema-conformant, which makes it exactly right for tests, CI and UI work — but it
**synthesises** its output rather than reading the contract, and mock embeddings are
not semantically meaningful. Clause attributes, risk scores and semantic search are
therefore demo-grade, not analysis anyone should rely on.

Because a mock extraction looks *identical* to a real one on screen, the system says
so loudly:

- a `mock_ai_providers_active` warning is logged on **every** start;
- `APP_ENV=production` **refuses to boot** on mock unless `ALLOW_MOCK_AI=true`
  records the decision deliberately.

## 12.3 The embedding model in use

| | |
| --- | --- |
| Model | `nvidia/nemotron-3-embed-1b` |
| Dimensions | **2048** — verified against the provider at startup |
| Storage | **`halfvec`** (see Chapter 5, Stage 7) |
| Index | HNSW, cosine similarity |
| Max input | 32,768 tokens (longer text is chunked upstream) |

**Asymmetric prefixes.** This model is trained with `query:` before a search string
and `passage:` before a document. Getting this backwards raises no error and returns
a perfectly valid vector — it just sits in the wrong part of the space, and recall
quietly drops with nothing to explain it. So the retrieval path calls `embed_query()`
and the indexing path calls `embed_many()`, and the provider applies the right prefix.

**Matryoshka truncation.** `EMBEDDING_DIM` may be lowered to 1024 or 512. The provider
slices the leading prefix and **re-normalises** — mandatory, because a slice of a unit
vector is not unit length, and mixing sliced with unsliced would make the sliced ones
score systematically lower. Anything above 2048 is rejected at startup; vectors are
never padded.

## 12.4 Changing the embedding model

Vectors from two different models are **not comparable**. A half-migrated index is
worse than an empty one — it keeps answering, from whichever vector space happens to
score higher. The supported procedure:

```bash
cip migrate                      # widen/retype the column, discard stale vectors
cip reindex-embeddings --all     # regenerate through the normal pipeline
```

`reindex-embeddings` is **resumable and idempotent**: the work list is derived from
the database (contracts holding a vector that is not the target model), so an
interrupted run picks up where it stopped and a second run is a no-op. Contracts are
re-queued from the `embedding` stage, so parsing/chunking/extraction keep their
checkpoints, progress shows on the Processing screen, and retries behave normally.
Semantic search is degraded for a contract until its re-index completes; **keyword
search is unaffected**.

\newpage

# Chapter 13 — Alerts, exports and dashboards

## 13.1 Alerts

Alerts have two halves, and one call does both: the alert is **saved as a row** (so it
appears in the Alerts screen and survives a restart) **and dispatched** to notification
channels (so somebody finds out without watching a screen).

| Alert type | Raised when |
| --- | --- |
| `contract_expiring` | A contract's expiry falls inside the configured window. |
| `auto_renewal_notice` | An auto-renewal notice deadline is approaching. |
| `high_risk` | A contract's risk score crosses the threshold. |
| `missing_mandatory_clause` | The profile requires a clause the contract does not contain. |
| `obligation_due` | An obligation's due date is approaching. |
| `processing_failed` | A pipeline stage failed terminally. |
| `review_required` | An extraction needs human review. |

**Notification channels:** console, email, Slack, Microsoft Teams, generic webhook,
generic HTTP.

Two behaviours worth knowing:

- **De-duplication is structural.** A `dedupe_key` is unique among *open* alerts, so a
  document that fails the same stage on three retries produces **one** row that gets
  updated — not three the operator has to dismiss.
- **Alerting can never fail the caller.** It is invoked from the pipeline's
  terminal-failure path, where an alerting exception would replace an accurate
  "stage X failed because Y" with a misleading alerting error — destroying exactly the
  evidence the operator needs. So every method returns rather than raises, and reports
  what it managed to do.

Stage failures are also categorised, so an alert says "embedding failed" with the
useful suggested fix rather than a generic "processing failed". Errors that mean the
*platform* is unwell (`database_error`, `storage_error`, `configuration_error`,
`queue_error`) escalate to CRITICAL — one bad PDF is routine, an unreachable database
is not.

## 13.2 Exports

Exports run **on the queue**, not in the request. A project-wide export across
thousands of contracts is not a 30-second HTTP response, and holding a database
connection and a request worker open for it starves everything else.

```
   POST /exports  ──►  create a row, return an id     (fast; the only part you wait on)
                            │
                            ▼
                     queue runs it:  load → render → upload to storage
                            │
                            ▼
   GET /exports/{id}  ──►  poll for status
                            │
                            ▼
   GET /exports/{id}/download  ──►  short-lived signed URL (audited every time,
                                    because an export is a copy of contract data
                                    leaving the platform)
```

Formats: **XLSX** today; CSV, JSON and PDF are drop-in via the same exporter
interface. Finished exports live for **48 hours** (keeping them forever turns every
export into a permanent second copy of contract data outside the contract's own
lifecycle). Download URLs live for **5 minutes** — the file is the durable artifact,
the URL is not.

## 13.3 Dashboards

`GET /dashboard` returns, scoped to the projects you can see:

- contract counts by status,
- risk band distribution,
- contracts expiring soon,
- top risks across the portfolio,
- upload trend over time,
- missing mandatory clause counts.

`GET /dashboard/processing` returns pipeline throughput, stage durations, queue depth
and failure rates.

Dashboard results are cached in Redis and the cache is **invalidated automatically**
whenever a contract in that project finishes processing — because everything derived
from that contract just changed.

\newpage

# Chapter 14 — Running and operating it

## 14.1 Local start

Requirements: **Podman** (or Docker) with its compose plugin. Nothing else — no
Python, no Node, no Postgres on your machine.

```bash
make podman-init          # start the podman machine (macOS/Windows); no-op on Linux
cp .env.example .env      # or: make env
make up                   # build + start the full stack
make logs                 # watch it come up
```

`make up` uses Podman when it is on PATH and falls back to Docker otherwise.
`make engine` prints which one it picked; `make up ENGINE=docker` forces the other.

| Service | URL |
| --- | --- |
| Frontend | http://localhost:5173 |
| API + Swagger | http://localhost:8000/docs |
| API ReDoc | http://localhost:8000/redoc |
| Queue introspection | http://localhost:9100/queues |
| MinIO console | http://localhost:9001 |
| Prometheus | http://localhost:9090 |
| Grafana | http://localhost:3001 |
| Jaeger (traces) | http://localhost:16686 |

Seeded administrator: `admin@irisregtech.com` / `Abc@1234` — **change this outside
local development.** `make nuke` tears everything down including volumes.

Podman is preferred where available because it is **rootless by default**: a
compromised container is confined to an unprivileged user rather than to root on the
host, which is the right default for a stack holding contract text.

## 14.2 What runs in the stack

| Container | Role |
| --- | --- |
| `postgres` | Database + pgvector. |
| `redis` | Cache, sessions, rate limits, queue backbone. |
| `minio` + `minio-init` | Local S3-compatible object storage, and its bucket setup. |
| `migrate` | Runs database migrations once, then exits. Everything else waits for it. |
| `backend` | The public API. |
| `queue` | The Node BullMQ dispatcher + workers. |
| `worker-parser` | Python worker pool for stages 1–5. |
| `worker-ai` | Python worker pool for stages 6–8. |
| `scheduler` | Reclaims stalled jobs; evaluates time-based alerts. |
| `frontend` | The React app. |
| `otel-collector`, `prometheus`, `grafana`, `jaeger` | Observability. |
| `embedding-check` | **Not a service — a gate.** Run under the `nvidia` profile; it probes the provider and exits non-zero if credentials are rejected or the dimension disagrees with the schema, so a bad config fails in seconds instead of on stage 7 of the first upload. |

## 14.3 Everyday commands

```bash
make lint             # ruff + mypy + eslint + tsc across all three packages
make format           # auto-format
make test             # backend (pytest) + frontend (vitest)
make test-integration
make migration m="add clause synonyms"    # autogenerate a migration
make migrate
make seed             # roles, admin user, clause master, document profiles
make psql
make redis-cli
make openapi          # dump openapi.json
make queue-status     # queue depths and dead-letter size
make smoke            # end-to-end smoke test against the running stack
make reprocess job=<uuid> stage=embedding
make diagnostics      # full system diagnostics report
make embedding-check  # probe the embedding provider before ingesting anything
make verify-pgvector  # compare the live vector schema against the live model
make reindex          # regenerate vectors after a model change
```

## 14.4 The operator CLI (`python -m app.cli`)

| Command | What it does |
| --- | --- |
| `migrate [--seed]` | Apply migrations, optionally seed reference data. |
| `downgrade <rev>` | Roll back. |
| `current` | Show the current migration revision. |
| `seed` | Seed roles, admin, clause master and profiles. |
| `scheduler` | Run the stalled-job sweeper loop. |
| `reprocess --job <id> --from-stage <stage>` | Re-run a pipeline stage. |
| `stages` | Show which stage handlers loaded, and why any failed. |
| `embeddings` | Probe the embedding provider; print the configuration. |
| `reindex-embeddings [--project-id] [--limit] [--dry-run]` | Resumable vector re-index. |
| `smoke` | End-to-end check. |
| `shell` | Python shell with app context. |
| `version` | Component versions. |

## 14.5 Health checks and what they mean

| Endpoint | Answers |
| --- | --- |
| `GET /healthz` | "Is the process alive?" — used by the container orchestrator to decide whether to restart. |
| `GET /readyz` | "Can it serve traffic?" — checks database, storage, queue, embedding config and stage handler availability. Used to decide whether to send traffic. |

**Startup philosophy — fail fast on configuration, tolerate transient outages:**

- A **bad JWT secret in production** stops the process. It will never be right.
- An **unreachable database at startup** leaves the process alive and failing
  readiness, so it recovers on its own without a restart loop.
- A **broken embedding configuration** stops the process — deliberately the one
  exception. A dimension that disagrees with its column, or 2048-wide vectors that
  cannot carry an index, does not *fail* requests; it silently returns worse answers
  for as long as it runs. Refusing to start is the only signal that cannot be ignored.

## 14.6 Observability

- **Structured JSON logs** with a correlation id and trace id on every record.
- **Prometheus metrics** at `/metrics` for the API, each worker pool and the queue.
- **OpenTelemetry traces** spanning API → queue → worker → AI provider. The trace
  context is captured at upload and attached to every job, so a stage running twenty
  minutes later still joins the original upload's trace.

What is measured:

| Area | Metrics |
| --- | --- |
| Pipeline | queue depth, stage durations, retries, checkpoint reuse, worker utilisation, throughput, cost |
| Retrieval | latency, strategy mix, candidate counts, re-rank latency, cache hit rate |
| RAG | latency, tokens, cost, citation rate, regeneration rate, confidence distribution |
| Security | authorization denials by reason |

\newpage

# Chapter 15 — Configuration

All configuration is environment-based. **No secrets in code.** `.env.example`
documents every variable. The ones that change behaviour most:

| Variable | What it controls |
| --- | --- |
| `APP_ENV` | `development` · `test` · `staging` · `production` (production enables the strict startup guards) |
| `DATABASE_URL` | PostgreSQL connection |
| `REDIS_URL` | Redis connection |
| `ACTIVE_PARSER` | `idoc` (default) · `pymupdf` · `adi` · `textract` · `googledocai` |
| `LLM_PROVIDER` / `LLM_MODEL` | Which AI reads and answers |
| `EMBEDDING_PROVIDER` / `EMBEDDING_MODEL` / `EMBEDDING_DIM` | Which AI builds the meaning index |
| `EMBEDDING_STORAGE` | `halfvec` (required above 2000 dimensions) or `vector` |
| `STORAGE_PROVIDER` | `azure` · `s3` · `minio` · `local` |
| `S3_ENDPOINT_URL` / `S3_PUBLIC_ENDPOINT_URL` | Internal vs browser-facing storage host (see Chapter 11.4) |
| `QUEUE_DRIVER` | `bullmq` (default) · `arq` |
| `WORKER_ROLE` | `all` · `parser` · `ai` |
| `RERANKER_ENABLED` | Cross-encoder re-rank on/off |
| `CONTEXT_TOKEN_BUDGET` | Context assembly token ceiling |
| `REVIEW_CONFIDENCE_THRESHOLD` | Human-review trigger (default 0.85) |
| `ALERT_EXPIRY_WINDOW_DAYS` | Expiring-contract alert window |
| `MAX_UPLOAD_SIZE_MB` / `MAX_FILES_PER_UPLOAD` | Upload limits (200 MB / 100 files) |
| `VIRUS_SCAN_ENABLED` / `CLAMAV_HOST` | Malware scanning |
| `JWT_SECRET`, `INTERNAL_API_TOKEN` | Secrets. Production refuses to start on the defaults. |
| `OIDC_ENABLED`, `AZURE_AD_*` | Microsoft single sign-on |
| `ORGANIZATION_LEGAL_NAMES` | Our own legal entity names — used to decide which contract party is "us" |
| `ALLOW_MOCK_AI` | Records a deliberate decision to run mock AI in production |

\newpage

# Chapter 16 — The design rules (and why each one exists)

These twelve rules are frozen. Every architectural decision in the backend traces
back to one of them.

| # | Rule | Why it exists — in plain English |
| --- | --- | --- |
| 1 | **Parser agnostic** | No vendor dependency escapes its adapter. If our PDF vendor doubles their price or goes down, we change one config value. |
| 2 | **Canonical Document Model** | One internal format, immutable once built. Without it, every downstream component would need to understand every parser's quirks. |
| 3 | **Stage isolation** | Each stage is independently scalable, retryable, replaceable and versioned. A slow AI stage should not force us to scale the PDF parser too. |
| 4 | **Artifact-driven** | Every stage output is a checkpoint *and* is reusable. This is what makes failure recovery cheap. |
| 5 | **Incremental processing** | A version change regenerates only what it affects. Editing a prompt should not re-parse a 300-page PDF or re-bill every embedding. |
| 6 | **Metadata-first retrieval** | Filter before you search. A repository of millions of contracts still answers fast. |
| 7 | **Hierarchical retrieval** | Document → clause → chunk. Narrow before you dig. |
| 8 | **Horizontal scalability** | Worker pools are stateless. Add more containers, get more throughput. |
| 9 | **Version everything** | Parser, CDM, chunk strategy, prompt, model, profile, index. So we can always answer "what exactly produced this fact?" |
| 10 | **Enterprise observability** | Structured logs, metrics, traces, health checks. A pipeline you cannot see is a pipeline you cannot operate. |
| 11 | **Project isolation** | Every derived row carries `project_id` and is filtered by it. The security boundary is enforced in data, not just in code paths. |
| 12 | **Explainable AI** | Every extraction and every answer carries its evidence. This is the difference between a tool a lawyer will use and one they won't. |

\newpage

# Chapter 17 — Frequently asked questions

**Q: If a contract fails at stage 6, do we re-do stages 1–5?**
No. Stages 1–5 have checkpoints. A retry starts at 6. This is the single biggest
reason the pipeline is cheap to operate.

**Q: What if someone uploads the same contract twice?**
The SHA-256 hash is computed *before* anything is stored. A duplicate is rejected
with a pointer to the existing contract, and no bytes are written. If you genuinely
want to replace it, use the `replace_existing` option — that creates a **new version**
and keeps the old bytes (earlier extractions reference them).

**Q: Can a Viewer in Project A see anything from Project B?**
No. Every derived row carries `project_id` and every read is filtered by it. If they
are not a member of B, the API responds "not found" — it will not even confirm that
project B exists.

**Q: What stops the AI from inventing a clause?**
Four things, layered. (1) Each category is given only pre-filtered relevant evidence,
never the whole document. (2) If no candidate evidence exists, we report "not found"
and make no AI call at all. (3) Every output is validated against a strict schema.
(4) For answers, any citation the model was not offered is detected and stripped
before display, and an uncited substantive answer is flagged for review.

**Q: Why does the risk score not come from the AI?**
Because a score a user cannot decompose is useless in a negotiation. Ours is derived
from named rules with stated weights, so "78 — High" can be broken down into the
individual findings that produced it, and a reviewer can argue with any one of them.

**Q: What is `needs_review` and who sets it?**
It flags a contract whose extraction a human should check. It is set when
classification confidence is below the profile's threshold, when a fallback profile
was used, when a mandatory clause is missing, when extraction categories failed, or
when a profile's own review rules fire. A flagged contract is **still fully indexed
and searchable** — the flag is about trusting the extraction, not about whether the
document can be found.

**Q: Can we add a new contract type?**
Yes, with **no code change**. Add a Document Intelligence Profile row: classification
hints, extraction prompts, mandatory clauses, chunking strategy, embedding levels,
risk mapping, compliance rules, review triggers.

**Q: Can we swap the AI vendor?**
Yes. `LLM_PROVIDER` and `EMBEDDING_PROVIDER` are independent config values. Changing
the *embedding* provider additionally requires a re-index (Chapter 12.4), because
vectors from two models are not comparable.

**Q: Why does the System Admin get "permission denied" on upload?**
That is intentional — see Chapter 7.3. Sign in as a project member, or ask one to
upload.

**Q: Where do I look first when something breaks?**

```
   1.  GET /readyz              → is a dependency down?
   2.  GET /jobs/-/health       → queue depths, stalled jobs, dead letters
   3.  GET /jobs/{id}/stages    → which stage failed, when, on which worker, and why
   4.  make logs                → structured logs; grep by the request id or job id
   5.  Jaeger (:16686)          → the full trace across API → queue → worker → provider
   6.  make diagnostics         → full system report
```

\newpage

# Appendix A — The complete state machine

```
                    ┌────────┐
                    │ QUEUED │
                    └───┬────┘
                        ▼
   ┌───────────► VALIDATING ──► PARSING ──► ENRICHING ──► CLASSIFYING
   │                                                            │
   │                                                            ▼
   │            INDEXING ◄── EMBEDDING ◄── AI_EXTRACTION ◄── CHUNKING
   │                │
   │                ▼
   │             READY  ✔
   │
   │            (from any running state)
   │                 │
   │      ┌──────────┼──────────┬───────────┐
   │      ▼          ▼          ▼           ▼
   │  RETRYING    FAILED    CANCELLED     PAUSED
   │      │                                  │
   └──────┘◄─────────────────────────────────┘
      re-queues the SAME stage,               resume continues from
      with exponential backoff                the last checkpoint
```

**Terminal states:** `READY`, `FAILED`, `CANCELLED`.
**Running states:** everything between `VALIDATING` and `INDEXING`.

Progress percentages are **weighted, not linear**, because parsing and extraction
dominate wall-clock time on a 150-page contract — an evenly-divided bar would sit at
25% for minutes and then jump:

| Stage | Progress range |
| --- | --- |
| Validation | 0 → 3% |
| Parser | 3 → 35% |
| Enrichment | 35 → 45% |
| Classification | 45 → 50% |
| Chunking | 50 → 58% |
| AI Extraction | 58 → 85% |
| Embedding | 85 → 95% |
| Indexing | 95 → 100% |

\newpage

# Appendix B — Where the code lives

```
backend/
  app/core/          config · logging · security · telemetry · errors · metrics ·
                     middleware · dependencies (auth/permissions) · enums · versions
  app/db/            async engine · session · base model · seed data
  app/models/        SQLAlchemy tables (the database schema in Python)
  app/schemas/       Pydantic request/response contracts (the API's shapes)
  app/api/v1/        routers — one file per domain
  app/api/internal.py  the stage endpoints the queue calls
  app/services/      domain services: upload · auth · user · project · contract ·
                     alerts · audit
  app/repositories/  data access (repository pattern — all SQL lives here)
  app/ai/
      parsers/       IDocumentParser + adapters (idoc, pymupdf, docx, mock) + OCR
      cdm/           Canonical Document Model + builder
      classification/  document classifier + profile selection
      chunking/      the chunking engine and its strategies
      extraction/    engine · prompts · schemas · validation · risk · evidence
      embedding/     providers · engine · pgvector · reuse · diagnostics · reindex
      graph/         knowledge graph builder
      retrieval/     planner · engine · context assembly
      rag/           providers · prompt orchestrator · answer engine + validator
  app/orchestrator/  workflow engine (decides WHAT) · runner (executes) ·
                     queue client · stages/ (the eight handlers)
  app/alerting/      dispatcher + console/email/slack/teams/webhook/http providers
  app/storage/       IObjectStorage + azure/s3/minio/local adapters
  app/export/        xlsx (now) · csv/json/pdf (drop-in)
  app/tools/         embedding probe · pgvector verifier · system diagnostics
  app/cli.py         the operator CLI
  app/main.py        the API application
  app/worker_app.py  the worker application (internal routes only)
  migrations/        Alembic versions
  tests/             unit + integration

queue/               BullMQ dispatch shim (Node) — no business logic
frontend/            React 18 + Vite + TypeScript + Tailwind + shadcn/ui
infra/               otel collector · prometheus · grafana · postgres init
docs/                this guide, plus alerting / artifact / embedding-migration notes
scripts/             API contract checks · deployment scripts
```

## The layering rule

Dependencies point **one way only**. Nothing lower may import from anything higher:

```
        api/  (routers — HTTP concerns only, no business logic)
          │
          ▼
      services/  (business logic, transactions)
          │
          ▼
    repositories/  (all SQL lives here)
          │
          ▼
       models/  (the tables)

      ai/  and  orchestrator/  hold no session and know nothing about HTTP.
      The engines (extraction, retrieval, RAG) never touch the database —
      the stage handlers and endpoints do the reading and writing.
      That is why every engine can be tested against a fixture with no
      database and no API key.
```

\newpage

# Appendix C — Quick reference card

```
 ┌──────────────────────────────────────────────────────────────────────────┐
 │  THE 8 STAGES                                                            │
 │  validation → parser → enrichment → classification →                     │
 │  chunking → ai_extraction → embedding → indexing                         │
 ├──────────────────────────────────────────────────────────────────────────┤
 │  THE 3 EMBEDDING LEVELS                                                  │
 │  L1 document_summary  →  which documents?                                │
 │  L2 clause            →  which clause?                                   │
 │  L3 chunk             →  which exact passage?                            │
 ├──────────────────────────────────────────────────────────────────────────┤
 │  THE 6 RETRIEVAL STRATEGIES                                              │
 │  metadata_only · metadata_plus_document · clause_retrieval ·             │
 │  chunk_retrieval · graph_traversal · hybrid (default)                    │
 ├──────────────────────────────────────────────────────────────────────────┤
 │  THE 4 ROLES                                                             │
 │  system_admin · project_manager · reviewer · viewer                      │
 │  (system_admin cannot upload — separation of duties)                     │
 ├──────────────────────────────────────────────────────────────────────────┤
 │  RISK BANDS                                                              │
 │  0–33 Low   ·   34–66 Medium   ·   67–100 High                           │
 ├──────────────────────────────────────────────────────────────────────────┤
 │  CONFIDENCE BANDS                                                        │
 │  ≥0.80 High   ·   ≥0.55 Medium   ·   below Low                           │
 ├──────────────────────────────────────────────────────────────────────────┤
 │  KEY LIMITS                                                              │
 │  200 MB per file · 100 files per upload · 5s upload response target      │
 │  2048 embedding dimensions · 200 candidate contracts · 6 chat turns      │
 │  48h export retention · 5min download URL · 15min file URL               │
 └──────────────────────────────────────────────────────────────────────────┘
```
