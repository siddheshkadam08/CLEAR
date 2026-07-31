# AI pipeline reliability

What changed in the inference, embedding, classification and chunking paths,
why, and how to operate them. Read this if you are tuning a profile, chasing a
bad extraction, or wondering why a contract came out empty.

---

## 1. Model routing

### The problem

Every LLM call resolved its model through `route_model(purpose)`, which
recognised two special-cased purposes and fell through to `settings.llm.model`
for everything else. So extraction, classification and summarisation - the bulk
of the pipeline's calls, none of which reason - all ran on the
reasoning-capable default. Not by decision: by omission from a frozenset.

There was also no way to answer "which model does clause extraction use?"
without reading the provider adapter.

### The design

`backend/app/ai/routing.py` holds one table:

```
LLMTask  ->  ModelTier  ->  model / effort / timeout
```

Three tiers:

| Tier | For | Configured by |
|---|---|---|
| `simple` | Extraction, classification, summarisation. Structured, high volume, no reasoning. | `LLM_MODEL_SIMPLE` |
| `complex` | Multi-document comparison, risk analysis, recommendations. | `LLM_MODEL_COMPLEX` |
| `reasoning` | Genuine chain-of-thought: legal reasoning, ambiguity resolution. | `LLM_MODEL_REASONING` |

Properties worth knowing:

- **`TASK_TIERS` is asserted complete at import.** Adding an `LLMTask` member
  without a tier raises immediately, so a new call site cannot silently inherit
  a tier nobody chose for it.
- **Unknown tasks route to `simple`, with a warning.** Defaulting an
  unrecognised task to the expensive tier turns a typo into a bill.
- **`reasoning` falls back to `complex`, never to `LLM_MODEL`.** An unset
  reasoning model should degrade to "strong", not to "whatever the generic
  default happens to be".
- **No model name exists outside `config.py`, `routing.py` and the provider
  adapters.** `test_model_routing.py` scans the tree and fails if one appears.

### Adding a task

1. Add the member to `LLMTask`.
2. Add it to `TASK_TIERS`. (Skip this and the import fails - deliberately.)
3. Pass it as `purpose=` at the call site.

Legacy `Purpose` strings still resolve through `LEGACY_PURPOSE_TASKS`, so
existing call sites did not have to change.

### Measured effect

`python -m scripts.benchmark_routing` prices one 28-page agreement's call
profile under both the old single-model policy and the tiered one, using
identical token volumes so the difference is attributable to routing rather
than provider variance.

```
TOTAL (one 28-page contract)          before $2.059    after $0.482
Saving: $1.577 per contract (76.6%)
```

`--live --samples N` additionally measures p50/p95/p99 latency per tier against
the configured provider. That part costs money, so it is opt-in.

---

## 2. Timeouts, retries and latency

`backend/app/ai/resilience.py`. Three decisions that matter:

**Retries are classified by exception type, not message text.** Matching on
message strings breaks the first time a provider rewords an error - and it
breaks by silently *not* retrying, which looks like a provider outage.

**Backoff uses full jitter.** `random.uniform(0, base * 2^attempt)`, capped.
Fixed backoff makes concurrent workers retry in lockstep and re-converge on
the exact rate limit they just backed off from.

**Timeouts are per tier.** One shared ceiling has to be set for the slowest
case, so a hung extraction held a worker slot for the reasoning-tier duration.

| Setting | Default | Applies to |
|---|---|---|
| `LLM_TIMEOUT_SECONDS_SIMPLE` | 90 | simple tier |
| `LLM_TIMEOUT_SECONDS_COMPLEX` | 300 | complex tier |
| `LLM_TIMEOUT_SECONDS` | 120 | reasoning tier, and the fallback |
| `LLM_RETRY_BACKOFF_SECONDS` | 1.0 | base delay before jitter |

There is also a deadline guard: if the next attempt cannot finish inside the
remaining budget, the call fails with the real error rather than timing out
mid-attempt and reporting a timeout instead of the cause.

### Metrics

| Metric | Labels | Question it answers |
|---|---|---|
| `cip_llm_task_duration_seconds` | task, tier, provider, model | p95/p99 per tier |
| `cip_llm_retries_total` | provider, tier, reason | is a provider degrading? |
| `cip_llm_timeouts_total` | provider, tier, model | is a tier's ceiling too low? |
| `cip_llm_payload_bytes` | direction, tier | has the evidence budget regressed? |

p95 for the simple tier:

```promql
histogram_quantile(0.95,
  sum by (le) (rate(cip_llm_task_duration_seconds_bucket{tier="simple"}[5m])))
```

---

## 3. Embedding spaces

### The problem

Cosine similarity is only meaningful between vectors from the same model.
Coordinates from two models are unrelated, so a distance computed across them
is arithmetic without semantics - it returns a number, ranks results by it, and
is wrong with nothing raised anywhere.

The store held 49 mock vectors alongside 2 real ones. Every search touching
them ranked against two unrelated spaces.

### The design

An `EmbeddingSpace` is the tuple that makes two vectors comparable:

```
(provider, model, dim, embedding_version, strategy_version)
```

`strategy_version` is in there because the *composed input* matters as much as
the model - changing what text gets embedded moves the vectors without changing
the model name.

`EmbeddingValidator.validate_batch(strict=True)` runs inside
`EmbeddingRepository.insert_many`, not in the stage that calls it. A foreign
vector is not a bad row that fails loudly: it inserts fine and then corrupts
every similarity search that touches the index. The check belongs where no
future caller can forget it. `insert_many(..., validate=False)` exists only for
the reindex path, which is deliberately writing a new space and has cleared the
old one.

Strict by default: a contract half-indexed in the wrong space is worse than one
that failed, because it answers queries and looks correct.

### Operating it

Check consistency:

```bash
cip embeddings
```

```
Vectors by model:
  mock-nvidia/nemotron-3-embed-1b: 49
  nvidia/nemotron-3-embed-1b:free: 2

  [FAIL] 49 vector(s) are not in openai:nvidia/nemotron-3-embed-1b:free@2048/v1:
    mock:mock-nvidia/nemotron-3-embed-1b@2048/v1: 49
```

Fix it:

```bash
cip reindex-embeddings --dry-run     # what would run
cip reindex-embeddings               # stale vectors only
cip reindex-embeddings --all         # everything, including current-looking rows
cip reindex-embeddings --project-id <uuid> --limit 50
```

Staleness is the *full* space identity, not just the model name. Comparing
model names alone meant a dimension change or a strategy bump left incomparable
vectors in place and reported nothing to do.

Safe to interrupt: the work list is derived from the database, so re-running
picks up what is left. Contracts are queued through the normal pipeline from
the embedding stage, so progress appears on the Processing screen and retries
behave as they do for any job.

Use `--all` when the vectors are suspect for a reason the provenance columns
cannot express - a provider that changed behaviour behind a stable model name,
or an index written before validation existed.

---

## 4. Classification

### The problem

A fallback reported `"No document type matched with sufficient confidence."`
regardless of what had actually happened - including when the tie-break model
was unreachable, which is not a property of the document at all.

A provider outage and an ambiguous contract need opposite responses: one is
retried once the service is back, the other needs a human or better hints.
They were indistinguishable.

### Fallback reasons

| Reason | Means | Do |
|---|---|---|
| `low_score` | Nothing scored above its threshold. | Review the document; consider hints. |
| `ambiguous` | Top two too close to separate. | Human review. |
| `llm_unavailable` | Tie-break model unreachable or errored. | **Restore the provider and reprocess.** Not a document problem. |
| `llm_invalid_response` | Model returned an unconfigured profile key. | Check the profile list and the prompt. |
| `llm_disabled` | Tie-break turned off for this run. | Expected, if intentional. |
| `forced_profile_missing` | Uploader pinned a key that is not configured. | **The requested profile was not used.** Fix the key. |
| `no_rules_configured` | No profile declares any `classification_hints`. | **Configuration fault.** Seed or configure the profiles. |

Fallbacks log at `warning`. Every fallback is a document processed under a
profile nobody chose; at `info` that scrolled past unnoticed while each
affected contract quietly received the default clause set.

### Result shape

The classification artifact now carries, alongside every original key:

```json
{
  "classification": "master_services_agreement",
  "confidence": 0.91,
  "top_predictions": [
    {"profile_key": "...", "score": 0.91,
     "matched_rules": ["required_phrases"],
     "matched_keywords": ["statement of work"],
     "blocked_by": []}
  ],
  "matched_keywords": ["statement of work"],
  "matched_rules": ["required_phrases"],
  "fallback_used": false,
  "fallback_reason": "none"
}
```

`top_predictions` is reported even on a confident match. When a classification
turns out wrong, the question is always "what was the runner-up and why did it
lose" - unanswerable after the fact unless it was recorded at the time.

`matched_keywords` holds the phrases themselves rather than `phrase:`-prefixed
labels, and `matched_rules` names the rule families that fired. Those are the
two questions asked when tuning a profile's hints.

### Metrics

| Metric | Labels |
|---|---|
| `cip_classification_fallback_total` | reason |
| `cip_classification_confidence` | method |

A rising `llm_unavailable` is an outage. A rising `ambiguous` means the
profiles' hints need work. Without the label they look identical.

---

## 5. Chunk rejection

### The problem

The stage reported "129 produced, 96 accepted" and discarded everything else.
Which rule fired, on which page, and how far off the threshold each chunk was
went into the bin with the chunk. Investigating "Extraction produced no
clauses" meant reproducing the run under a debugger.

### What is recorded

Every rejection now carries page, chunk type, token and character counts,
section title, a text preview, and the `RejectionRule` that fired:

| Rule | Fires when |
|---|---|
| `empty_text` | No text content. |
| `below_min_tokens` | Under `min_tokens`. |
| `above_max_tokens` | Over twice `max_tokens` with no child chunks. |
| `ends_mid_clause` | Text ends mid-clause. |
| `section_produced_no_text` | A section yielded nothing, so no chunk was built. |

That last one was previously invisible: the builder returned `None` and moved
on, so the content vanished without appearing in any count. It made a parser
returning empty sections indistinguishable from a document that genuinely has
none.

Reason and rule are separate. One reason has several causes - `too_small` fires
for a boilerplate fragment and for a page of OCR noise - and those need
different fixes.

The report aggregates `by_rule`, `by_page`, `by_chunk_type` and reports
`worst_pages`. Page clustering is the tell: rejections spread evenly are
ordinary boilerplate; forty on one page mean that page parsed badly, and no
amount of threshold tuning will help.

### Replay

```bash
cip replay-chunking <contract-id>
cip replay-chunking <contract-id> --sweep
cip replay-chunking <contract-id> --min-tokens 20 --strategy clause_based
```

Reads the stored canonical document and runs the engine **in memory**. Nothing
is written, so it is safe against production data and safe to repeat - which is
the point. Testing a threshold previously meant a full reprocess per attempt,
so in practice thresholds were never tuned.

Real output from a contract in the store:

```
hybrid  min=40 max=800  ->  5 kept, 3 rejected (62% accepted)
    below_min_tokens: 3
    worst pages: p2(3)

  p2 clause [below_min_tokens] 20 < 40 tokens
    Either party may terminate this Agreement for material breach upon thirty

  p2 clause [below_min_tokens] 37 < 40 tokens
    7. CONFIDENTIALITY  Each party shall protect the other's Confidential...
```

The termination and confidentiality clauses - two of the ones extraction most
needs - were being discarded for being 20 and 37 tokens against a minimum of
40. `--sweep` shows `min_tokens=20` keeps all 8 chunks where 40 keeps 5.

---

## 6. Secrets

`python scripts/audit_secrets.py` scans every git-tracked file and exits
non-zero on a finding. It runs in CI as its own job, before anything is
installed, so an install failure cannot skip it.

Two rule kinds. **Shape rules** match credential formats that are
unmistakably real - prefix plus length, so `sk-ant-xxx` does not trip them.
**Assignment rules** match `*_PASSWORD=value` where the value is not a
placeholder; this is the one that catches ordinary mistakes.

"Placeholder" is defined generously on purpose. An audit tool with false
positives gets switched off, and a switched-off tool finds nothing. If a
finding is a false positive, use a value the script recognises - see
`PLACEHOLDERS` and `PLACEHOLDER_MARKERS`.

**A credential that was ever committed is compromised.** Rotate it. Deleting it
from the working tree leaves it in the history.

---

## 7. Deploy preflight

`scripts/deploy.sh` refuses to bring up a stack configured with mock providers
or the fixture parser.

These exist so tests and offline work are possible without a key, and
`.env.example` carries them as the *local* default for exactly that reason.
Deployed, they are indistinguishable from the real thing at a glance: the
pipeline runs green, contracts reach READY, and every clause, date and party in
the output was synthesised rather than read from the document. Nothing
downstream can tell, so it has to be caught before the stack comes up.

```bash
ALLOW_SIMULATED_AI=1 ./scripts/deploy.sh    # deploy one deliberately
```

---

## Quick reference

```bash
cip embeddings                      # embedding config + space consistency
cip reindex-embeddings [--all]      # re-embed into the configured space
cip replay-chunking <id> [--sweep]  # test chunk thresholds, writes nothing
python -m scripts.benchmark_routing # routing cost, before vs after
python scripts/audit_secrets.py     # credential scan over tracked files
```
