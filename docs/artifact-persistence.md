# Artifact persistence and round-trip validation

Pipeline stages do not pass documents to each other. Each stage writes its output
to object storage and records a pointer in `document_artifacts`; the next stage
reads it back. Queue messages stay small and safe to log, and a re-delivered
message always reads the current artifact instead of a stale copy.

That design has one hard requirement, and it is easy to miss:

> **Whatever a stage writes, the next stage must be able to read.**

---

## The bug this suite exists to prevent

`CdmBase` sets `extra="forbid"`. `Page.block_count`, `QualityMetrics.is_degraded`
and `CanonicalDocument.full_text` are `@computed_field` properties.

Pydantic **emits** computed fields on `model_dump()` and **rejects** them on
`model_validate()` when extras are forbidden. So every CDM artifact written to
storage was unreadable:

```
The parser artifact is not a valid normalized document: 3 validation errors
  pages.0.block_count   Extra inputs are not permitted
  pages.1.block_count   Extra inputs are not permitted
  quality.is_degraded   Extra inputs are not permitted
```

Every upload died at enrichment. Not on one parser — on **all** of them, since
the fault was in the shared model rather than in any adapter. `CanonicalDocument`
carried the same fault one stage later, at chunking.

The whole test suite passed throughout. Every test held a model in memory and
asserted on it; not one sent a model through storage and back.

### The fix

A `mode="before"` validator on `CdmBase` strips exactly the model's own computed
field names on input. `extra="forbid"` keeps its real job — catching genuinely
unknown keys — while a model can load its own output.

Dropping the computed keys rather than relaxing to `extra="ignore"` is the
distinction that matters: `ignore` would also have silently swallowed a typo'd
field name or a vendor key that should have been mapped.

---

## What the suite covers

`backend/tests/integration/test_artifact_roundtrip.py` exercises the real path:

```
build → model_dump → orjson → object storage → read → model_validate → use
```

**Regression, and its generalisation.** The three known computed fields are
pinned individually. Then two *reflective* tests enumerate CDM models at runtime
and assert that every model reloads its own dump, and that `extra="forbid"` still
rejects unknown fields. A computed field added next year is covered automatically
— no one has to remember to write the test. A guard test fails if the set of
models carrying computed fields changes, so the change is at least deliberate.

**Type fidelity.** UUIDs, timestamps, floats, Unicode, nested objects, lists and
their order, free-form metadata dicts, empty collections, optional fields.

Two findings worth knowing:

- CDM identifiers and timestamps are carried as **strings**, not `UUID`/`datetime`.
  The artifact is a wire format read by more than Python. The tests assert the
  string survives *and* still parses back into the richer type, and pin the
  boundary where a malformed identifier actually fails.
- `put_json` uses `OPT_SORT_KEYS` so checksums stay comparable across
  regenerations. Literal field order is therefore unstable **by design**, so the
  invariant asserted is that validation is order-independent.

**Negative cases.** Corrupted and truncated JSON, missing required fields at root
and nested, wrong types, invalid enums, unknown fields at root and nested, wrong
root type, missing artifacts. Also that a *stale or malformed computed value*
cannot poison a load — it is discarded and recomputed, never trusted.

**Storage backends.** A shared conformance assertion runs against every backend.
Filesystem-backed runs always; S3 and Azure run under
`TEST_STORAGE_PROVIDER=s3|azure`, matching how the live-database tests skip.

**Performance.** Small, multi-page and large artifacts, plus a scaling check that
10× the pages does not cost 100× the time. Thresholds are deliberately generous —
this guards against an accidental O(n²) or per-node revalidation, not a benchmark,
and has to stay green on a loaded CI box.

---

## Running it

```bash
pytest tests/integration/test_artifact_roundtrip.py          # hermetic
pytest tests/integration/test_artifact_roundtrip.py -m slow  # include performance
TEST_STORAGE_PROVIDER=s3 pytest tests/integration/test_artifact_roundtrip.py
```

---

## Adding a computed field

Add it. The reflective tests pick it up on the next run.

If `test_every_cdm_model_with_computed_fields_is_covered` fails, that is the
guard doing its job: confirm the new field round-trips, then update the expected
set. Do not delete the assertion — it is what makes the coverage claim true
rather than aspirational.
