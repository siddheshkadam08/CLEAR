# Retrieval evaluation framework

The Copilot has four tunable similarity thresholds, a confidence heuristic, a
document-type confidence gate, a pluggable re-ranker and a context budget. Every
one of them was set by argument. This framework is what turns the next argument
into a measurement.

Its organising rule: **nothing here mocks the pipeline.** The runner drives the
real `CopilotService` against a real database. What is scored is what a user
would have received.

---

## Architecture

```
                    ┌──────────────────────────────────────────┐
   golden/*.json    │             dataset                      │
   golden/*.jsonl   │  models · loader · bootstrap             │
        │           └───────────────────┬──────────────────────┘
        │                               │ GoldenDataset
        ▼                               ▼
┌───────────────────────────────────────────────────────────────┐
│                          runner                               │
│  EvaluationRunner.run(dataset)                                │
│                                                               │
│    per case, bounded concurrency, own DB session:             │
│                                                               │
│    CopilotService.prepare ──▶ analysis ──▶ plan ──▶ retrieve  │
│                                   │                    │      │
│                              rerank ◀──────────────────┘      │
│                                   │                           │
│                              guardrail ──▶ assemble           │
│                                                │              │
│    CopilotService.answer  ─────────────▶ generate             │
│                                                               │
│    flattened to  CaseResult { retrieved[], citations[],       │
│                               plan, timings, cost }           │
└───────────────────────────┬───────────────────────────────────┘
                            │ RunResult  (serialisable, re-scoreable)
                            ▼
┌───────────────────────────────────────────────────────────────┐
│                          metrics   (pure functions)           │
│                                                               │
│  relevance ──▶ retrieval   recall@k · precision@k · MRR       │
│      │                     nDCG · duplicate rate              │
│      ├───────▶ citation    precision · recall · broken        │
│      │                     hallucinated · uncited             │
│      ├───────▶ planner     intent · doc-type · false filter   │
│      ├───────▶ guardrail   confusion matrix · L1 would-pass   │
│      ├───────▶ calibration ECE · MCE · Brier · Platt · iso    │
│      └───────▶ performance latency percentiles · cost         │
│                          │                                    │
│                          ▼                                    │
│                      Scorecard  +  composite                  │
└───────────────────────────┬───────────────────────────────────┘
                            │
          ┌─────────────────┼──────────────────┐
          ▼                 ▼                  ▼
   ┌────────────┐   ┌──────────────┐   ┌────────────────┐
   │  baseline  │   │   reports    │   │   benchmark    │
   │  compare   │   │ json · csv   │   │ sweep          │
   │  gates     │   │ md · html    │   │ ablation       │
   │  verdict   │   │ charts       │   │ history        │
   └─────┬──────┘   └──────────────┘   └────────────────┘
         │
         ▼
   exit 0 / 1 / 2   ──▶  CI
```

Exit codes are the contract with CI: **0** the gate passed, **1** it failed,
**2** the run could not execute. The third matters — a build must not read "the
database was unreachable" as "quality regressed".

---

## Quick start

```bash
cd backend

# What is available, and what each set can actually score
python -m app.evaluation.cli dataset list
python -m app.evaluation.cli dataset validate smoke

# Retrieval only: deterministic, no model credentials, ~10x cheaper
python -m app.evaluation.cli benchmark --dataset smoke --retrieval-only

# The full thing, including generation, citations and cost
python -m app.evaluation.cli benchmark --dataset banking-msa

# Establish the reference. Deliberate, reviewable, never automatic.
python -m app.evaluation.cli benchmark --dataset banking-msa --update-baseline
```

Reports land in `evaluation-results/<label>/`:

| File | For |
| --- | --- |
| `summary.json` | CI and the dashboard — small, just the verdict and the metrics |
| `evaluation.json` | Everything, including per-case detail. Re-scoreable. |
| `metrics.csv`, `cases.csv` | Spreadsheet triage |
| `REPORT.md` | Pasting into a pull request |
| `*_report.html` | Seven self-contained pages with charts |

---

## Building a golden dataset

This is the work. The framework is worthless without one, and a good one is
worth more than any amount of framework.

### Shape

```json
{
  "id": "acme-msa-notice",
  "question": "What is the termination notice period?",
  "projectId": "…",
  "contractId": "…",
  "expected": {
    "contracts": ["…"],
    "clauses": ["…"],
    "pages": [18],
    "headings": ["Termination"],
    "agreementTypes": ["msa"],
    "shouldAnswer": true,
    "intent": "clause_lookup",
    "documentType": "MSA",
    "answerContains": ["thirty days"]
  },
  "tags": ["msa", "banking", "termination"]
}
```

Every field under `expected` is optional. An empty collection means *no
expectation on this dimension*, not *expected to be empty* — writing complete
expectations for a thousand questions is work nobody does, so partial ones are
first-class.

### Bootstrapping

```bash
python -m app.evaluation.cli dataset generate \
  --project-id <uuid> --output app/evaluation/golden/banking-msa.jsonl --per-contract 3
```

This writes real ids, headings and page numbers with **placeholder questions**.
A generated question would test whatever the generator understood from the same
text the pipeline is being asked to find — circular. Someone writes the
questions.

### Three rules

1. **At least a third negative cases** (`shouldAnswer: false`). A set of only
   answerable questions measures recall and nothing else — it cannot see a
   guardrail regression, which is the one that lets the platform invent contract
   terms. Include plausible-but-absent subjects and non-existent clause numbers.
2. **Tag everything.** Metrics are sliced by tag, which is how a regression
   confined to NDAs is seen before it is averaged away across ten thousand cases.
3. **Version the dataset when you change it.** Baselines are keyed on
   `name@version`. Adding fifty questions changes what every average means, and
   comparing across that boundary produces a report about the dataset rather than
   about the code.

### Scale

JSONL for anything above a few hundred cases: it diffs per case and streams.
A ten-thousand-case JSON array is a file no reviewer can read and no editor will
open.

---

## What each metric means

### Retrieval

| Metric | Reading |
| --- | --- |
| `recall@k` | Expected units found in the top k. Units, not passages — three chunks of one clause is one unit. |
| `precision@k` | Relevant share of what came back. Denominator is what was returned, so three-for-three is 1.0. |
| `mrr` | 1/rank of the first hit. **The one that matters most** — the context budget only admits the first handful, so a hit at rank 40 is one the model never saw. |
| `ndcg@10` | Graded: distinguishes "the exact clause, first" from "the right contract, first". |
| `duplicate_rate` | Share of retrieved passages repeating text already present. Each one costs a context slot. |

Relevance is graded, and precedence is strict: a case naming clause ids is judged
on clause ids. Union would let every passage from the right contract count,
inflating recall for exactly the cases whose authors were most precise.

### Citations

Four distinct failures, because they have different causes:

- **Hallucinated** — a label the model invented. Stripped before display, so
  invisible in production and countable only here. A rise is the earliest signal
  that a model change degraded grounding.
- **Broken** — resolved, but points at a passage retrieval did not return.
  Structurally impossible via the validator's allow-list. **A non-zero count is a
  defect in our code**, which is why its gate has an absolute limit of zero.
- **Imprecise** — real citation, real passage, not what the case expected.
- **Missed** — an expected clause that was shown to the model and not cited.

`hallucination_rate` = (fabricated labels + substantive uncited answers) ÷ answers.

### Guardrail

A confusion matrix over `shouldAnswer`. **False accept is the dangerous
quadrant**: the platform produced contract terms for a question the corpus cannot
support. False reject is costly but safe.

`document_summary_would_have_passed` counts cases where an L1 summary cleared the
answer threshold while no clause or chunk did. Each would have passed a guardrail
computed on the whole-result maximum — the per-level fix, measured rather than
assumed.

### Confidence calibration

The platform's confidence is a heuristic that varies in the right direction. It
is displayed as a percentage, which invites reading it as a probability, and this
is what checks whether that holds.

- **ECE** — mean gap between confidence and observed accuracy, population-weighted.
- **MCE** — the worst single bin. Matters more than ECE for a reviewer, who
  experiences one answer at a time.
- **Brier** — read against the base rate; on a 90%-correct set, 0.09 is unimpressive.
- **Reliability diagram** — bars below the diagonal are over-confidence, the
  harmful direction.

Platt and isotonic calibrators are fitted and scored. **Runtime confidence is not
changed.** A figure that silently changed meaning between releases would be worse
than one never calibrated. Isotonic wants ≥1000 samples; below that, Platt's two
parameters are the safer fit.

---

## Sweeps and ablations

```bash
# One parameter
python -m app.evaluation.cli sweep --dataset banking-msa --parameter min_similarity_clause

# Every default sweep
python -m app.evaluation.cli sweep --dataset banking-msa

# Is the re-ranker worth its latency and cost?
python -m app.evaluation.cli ablate reranker --dataset banking-msa
```

Sweeps are retrieval-only by default and run **one parameter at a time**, not a
grid: a full grid over five parameters at five points is 3,125 dataset runs,
which nobody waits for and which mostly measures interactions that do not exist.

The winner is chosen by the composite score, whose weights are in
`metrics/scorecard.py` in the open. Optimising a single metric would find the
threshold that maximises recall by retrieving everything.

The re-ranker ablation reports gain **against its cost** — two points of recall
for two seconds and a doubled bill is not obviously worth enabling, and a
comparison reporting only the recall would say it was.

### Embedding models

```bash
python -m app.evaluation.cli ablate embedding --models arms.json
```

**This does not re-embed the corpus.** Vectors from two models do not share a
space, so a meaningful comparison needs the index rebuilt per arm
(`cip reindex-embeddings`). Run it without doing so and every arm but the live
one scores near zero — a real result about mismatched vectors, not a comparison
of models.

---

## The regression gate

| Metric | Direction | Tolerance | Blocking |
| --- | --- | --- | --- |
| `recall@5`, `recall@10`, `mrr`, `ndcg@10` | higher | 0.02 | yes |
| `citation_precision` | higher | 0.03 | yes |
| `hallucination_rate` | lower | 0.01 | yes |
| `broken_citations` | lower | 0 (absolute) | yes |
| `guardrail_accuracy` | higher | 0.03 | yes |
| `false_accept_rate` | lower | 0.02 | yes |
| `latency_p95_ms` | lower | 250 ms or 20% | yes |
| `mean_cost_usd` | lower | $0.0005 or 25% | yes |
| `composite` | higher | 0.02 | yes |
| planner, calibration, duplicate rate | — | 0.05 | watched |

Three properties are deliberate:

- **Direction is declared, never inferred.** A gate that guessed from the name
  would eventually guess wrong, in silence.
- **Tolerance is absolute except for latency and cost.** A relative tolerance on
  a metric near zero is meaningless — 0.001 to 0.002 is a 100% regression and
  nothing at all.
- **A missing metric fails.** If the baseline names a metric the run did not
  produce, the report shape changed, and passing on the grounds that the number is
  absent is how a gate quietly stops gating.

Baselines live in `backend/app/evaluation/baselines/<dataset>-<version>.json` and
are moved by hand, in a reviewable commit. Automatic re-baselining ratchets a slow
decline into the reference and stops failing.

---

## CI

`.github/workflows/evaluation.yml`:

- **Every PR** touching the retrieval path → metric self-test, then a
  retrieval-only benchmark. Deterministic, no credentials, minutes.
- **Nightly** → the full benchmark with generation, which is what scores citation
  quality, hallucination rate and cost.

The report is uploaded on `always()` — a failing gate is when it matters most —
and posted as a single updated PR comment rather than one per push.

The metric self-test runs first and on every trigger. A gate whose arithmetic is
wrong is worse than no gate.

---

## Load testing

Separate from the benchmark, because it answers a different question. It needs a
live target and a real token, and it is not wired into the quality gate.

```bash
python -m app.evaluation.cli load-test \
  --base-url https://clear.internal --token "$TOKEN" \
  --users 100,500,1000,5000 --duration 60
```

Errors are reported, never retried — a retry hides exactly the saturation the
test exists to find. Saturation is called on the *first* level that breaches a
bound, not the worst: past the knee every number describes a queue rather than
the system.

---

## Observability

Spans per stage (`copilot.analysis`, `copilot.retrieval`, `copilot.assembly`,
`copilot.generation`) with intent, strategy, similarity and mode as attributes.
"The Copilot is slow" resolves to a flame graph rather than a guess.

Prometheus:

| Metric | Type |
| --- | --- |
| `cip_copilot_queries_total{outcome,retrieval_mode}` | counter |
| `cip_copilot_answerable_similarity` | histogram |
| `cip_copilot_context_chunks` | histogram |
| `cip_evaluation_metric{dataset,metric}` | gauge |
| `cip_evaluation_runs_total{dataset,result}` | counter |

The evaluation gauges are written by a benchmark run, so they are only useful
from a long-lived process — the scheduled in-cluster run, not a CLI invocation
that exits before the next scrape.

---

## Known limits

Stated because a benchmark that oversells itself is worse than none.

1. **Generation is not deterministic.** Retrieval is; answer text varies between
   runs even at temperature zero. The gate is therefore built around retrieval and
   citation metrics, and treats answer-text checks as secondary.
2. **Correctness labels are a proxy.** A case is "correct" when the answer cited
   the evidence the case named. That measures *retrieval* correctness, not *legal*
   correctness — a calibration curve here does not say the answers are right.
3. **The bundled `smoke` set scores nothing real.** Its ids are synthetic. It
   proves the harness works; a real dataset proves the platform does.
4. **Cost is provider-reported.** A model with no pricing entry attributes zero
   rather than a guess, so `$0.00` means unpriced, not free.
5. **`by_tag` omits segments under three cases.** An average over two is noise
   presented as a trend.
