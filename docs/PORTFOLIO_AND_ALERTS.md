# Portfolio and Alerts — feature reference

The two features that turn extracted knowledge into something a person acts on. Portfolio
is the **pull** view — go and look across every contract. Alerts are the **push** view —
be told when a date arrives.

Every field, threshold and rule below is taken from source.

> **Not to be confused with `docs/alerting.md`.** That covers *operational* alerting — a
> stage failed, a worker died — delivered to Slack, Teams or a webhook. This covers
> *contract* alerts: expiries, renewals, risk, obligations. Different code, different
> trigger, different audience. See [Two alert systems](#two-alert-systems).

---

# Part 1 — Portfolio

## Why it exists

Extraction has always produced these rows, and the contract detail screen has always shown
them — for one agreement. That answers *"what is in this contract?"* and nothing else. It
cannot answer:

* *what falls due this month?*
* *which counterparty carries the most exposure?*
* *where are the uncapped liability clauses?*

which is what a repository is kept for. Portfolio reads the same rows **across every
contract the caller can see**.

One screen with four tabs rather than four nav entries: they share a scope selector and a
mental model — "the register".

## Three rules that hold on every tab

**1. Scope is your project memberships.** `project_id` only ever *narrows* that set. An
empty scope short-circuits to an empty page rather than issuing an unbounded query — a
user with no memberships must not be one missing `WHERE` clause away from the whole
estate.

**2. Archived and failed contracts are excluded.** A register lists things somebody may
have to act on. An obligation under an archived contract is history, and mixing the two
makes the live rows harder to trust. `failed` means extraction never completed, so its
rows are partial at best.

**3. Unresolved rows are shown, not hidden — and every row links back to its contract.**
The register is a pointer; the evidence and the page highlight live on the document.

That third rule is the one that shapes the data model, and it recurs below.

## Obligations

**A duty the contract imposes: who must do what, by when, triggered by what.** Extracted
from the text, each carrying evidence back to the clause it came from.

| Field | Meaning |
|---|---|
| `action` | the duty itself — *"deliver the audited accounts"* (required) |
| `responsible_party` | who owes it |
| `due_date` | the calendar date — **when one exists** |
| `due_description` | the deadline in words — *"within 30 days of termination"* |
| `trigger_event` | the event that starts the clock |
| `frequency` / `is_recurring` | quarterly reporting vs a one-off |
| `status` | open · in_progress · fulfilled · breached · waived · unknown |
| `penalty` | what happens if it is missed |
| `clause_id` | the clause it was found in |

### The design decision worth understanding

`due_date` and `due_description` are **two separate columns, deliberately** — *"kept
alongside `due_date` rather than forcing a guess."*

A contract saying *"within 30 days of invoice"* has a real deadline but not a calendar
one; extraction cannot resolve it without knowing the invoice date. Most systems would
either invent a date or drop the row. This does neither: it stores the phrase, leaves
`due_date` NULL, **sorts the row last, and gives it its own filter**.

So `undated=true` is not an edge case — it is **a review queue**, the obligations somebody
must convert into real dates by hand. Dropping them *"would silently shrink the very list
this screen exists to be complete about."*

### Filters

| Filter | Behaviour |
|---|---|
| `status` | repeatable — `?status=open&status=breached` is OR |
| `responsible_party` | partial, case-insensitive |
| `due_from` / `due_to` | date window; only matches rows that *have* a date |
| `undated=true` | only unresolved deadlines — the review queue |
| `undated=false` | only dated obligations — the schedulable ones |
| `q` | partial match on the obligation text |

Sort order is soonest first, undated last.

## Key dates

The calendar behind the estate, earliest first.

| Field | Meaning |
|---|---|
| `date_type` | effective, execution, expiration, renewal, notice deadline, milestone, payment due, delivery, review, termination, commencement, other |
| `date_value` | the resolved calendar date |
| `date_expression` | the relative phrase, when it could not be pinned |
| `description`, `is_recurring` | |

Same split as obligations, same reasoning. Rows with only a `date_expression` sort last
and are reachable with `unresolved=true` — *"they are the ones most likely to matter and
least likely to be noticed."*

**Filters:** `date_type` (repeatable), `unresolved`, `date_from` / `date_to`.

> ### Known gap: five date types have no filter chip
>
> The UI offers seven chips — expiration, renewal, notice deadline, payment due,
> milestone, review, termination. The enum has twelve. **`effective_date`,
> `execution_date`, `delivery_date`, `commencement_date` and `other` cannot be isolated by
> any chip.** They appear in the unfiltered list; nothing selects them.
>
> It reads as deliberate curation — the seven shown are all forward-looking deadlines,
> which is what the tab is for — but unlike `OBLIGATION_STATUSES` there is no comment
> saying so. Either add the chips or record the reason.

## Risks

Every finding across the estate, most severe first.

| Field | Meaning |
|---|---|
| `risk_type`, `category` | classification |
| `severity` | critical · high · medium · low |
| `description`, `recommendation` | the finding and what to do |
| `score_contribution` | what this finding added to the contract's score |
| `is_omission` | **the finding is about something absent** |
| `contract_risk_score` | the parent contract's overall score |

**`omissions=true` is the filter worth knowing.** These are findings about what the
contract *does not say* — a missing liability cap, no termination right. An absent clause
is invisible when reading a document, which is exactly why it gets its own filter.

Ordering is by severity, then by the contribution the finding made to its contract's
score, so the top of the list is what a reviewer should read first *"rather than whichever
contract happened to be uploaded most recently."*

Severity ordering uses an **explicit rank**, not `ORDER BY severity`: the column is a
native Postgres enum, so the database would sort it by declaration order — *"correct
today, and silently wrong the moment somebody inserts a member into the middle."*

**Filters:** `severity` (repeatable), `omissions`, `q` (matches description *and*
recommendation), plus API-only `risk_type` and `category`.

## Counterparties

Who you contract with, and how often.

| Field | Meaning |
|---|---|
| `name`, `key` | display name, and the lower-cased grouping key |
| `entity_types`, `roles`, `jurisdictions` | |
| `contract_count` | **distinct contracts**, not mentions |
| `is_primary_anywhere` | signs somewhere, rather than named in passing |
| `total_value` | exposure, per currency |
| `next_expiry` | the soonest expiry across their contracts |

### Two honest limitations, stated in the code

**Grouping is not entity resolution.** Aggregated by lower-cased name, so *"Acme Corp"*
and *"Acme Corporation Inc."* are one counterparty to a lawyer and **two rows here**.
Merging them needs a confirmable match, and *"a directory that guessed would under-report
exposure — which is the one number this screen exists to give."*

**`contract_count` counts contracts, not mentions.** A party named six times in one
agreement is one contract; counting mentions *"would make a verbose document look like a
major relationship."*

**Filters:** `q`, `primary_only`.

---

# Part 2 — Contract alerts

## Two alert systems

| | Operational alerting | Contract alerts *(this document)* |
|---|---|---|
| Code | `app/alerting/` | `app/services/alert_evaluator.py` |
| About | the **platform** — a stage failed, a worker died | the **contracts** — expiry, renewal, risk, obligations |
| Raised by | the code path where the failure happens | a periodic sweep |
| Delivered to | Slack · Teams · webhook · email · console | in-app, plus opt-in channels per rule |
| Documented in | `docs/alerting.md` | here |

They share the word "alert" and nothing else.

## Why a sweep

Nothing pushes these conditions — *"they arrive by the calendar moving."* An expiry ninety
days out becomes an expiry sixty days out because time passed, not because anyone did
anything. So something has to look.

**This sweep is the only thing that raises these alerts.** Without it the Alerts screen
stays empty and nothing errors — which is the failure mode worth knowing about.

## The seven types

| Type | Condition | Configurable |
|---|---|---|
| `contract_expiring` | Term ends inside the warning window. **Already-expired contracts are excluded** — that is a different condition, needing a decision about the record rather than a deadline reminder, and including them would alert on every historical contract the first time it ran | `window_days` (90), `escalate_days` (30) |
| `auto_renewal_notice` | An auto-renewing contract whose notice window is closing. Uses the extracted `notice_deadline` when there is one, otherwise counts back from expiry — *"miss that date and the term renews whether or not anybody intended it"* | window; longer lead time than plain expiry, because the notice deadline falls **before** the expiry date |
| `high_risk` | Risk score at or above the cutoff. **No date in the dedupe key** — a standing condition rather than a deadline, so a re-scored contract refreshes the same alert instead of accumulating one per sweep | `risk_score_cutoff` (67) |
| `missing_mandatory_clause` | Mandatory clause types extraction did not find | `clause_types` — empty means *whatever the contract's profile marks mandatory* |
| `review_required` | **Two independent triggers**: the contract-level `needs_review` flag set by the pipeline's own confidence checks, **or** clauses queued by the profile's review rules. Either one means somebody has to look | `min_items` (1) |
| `obligation_due` | An unfulfilled obligation falling due, **or recently missed**. Overdue ones are included — *"an obligation register that cannot tell you what is late is not much of a register"* — but bounded, so a contract from four years ago does not resurface forever | `window_days` (14), `overdue_days` |
| `processing_failed` | **Never evaluated by the sweep.** Raised by the orchestrator, which knows things the sweep cannot | `after_retries` (3) |

`processing_failed` deserves its emphasis: including it in the sweep would make the retire
step *"resolve every processing failure the moment a sweep ran."* A rule row still exists
for it, so severity and channels stay configurable — the orchestrator raises it, the
sweep never touches it.

### How the six are evaluated

Four are driven by a mapping over each contract — expiring, auto-renewal, high risk,
missing clause — because they all ask a question about one contract and its metadata.
`review_required` and `obligation_due` are called separately: the first needs the pending
clause count alongside the contract, and the second iterates **obligations**, not
contracts. Same sweep, different row sets.

## Three things happen per sweep, in this order

**1. Raise** — a condition that holds and has no alert yet becomes one.

**2. Refresh** — a condition that holds and already has an open alert updates that row in
place. *"Ninety days to expiry becoming sixty is new information about the same fact, not
a second alert."*

**3. Retire** — an alert whose condition no longer holds is resolved. Without this *"a
renewed contract keeps warning about an expiry that has been dealt with, and the screen
fills with things nobody can action."*

De-duplication is by `dedupe_key`, **unique among open alerts at the database level** —
not merely checked in code. The keys embed the date the alert is about, so a corrected
expiry date retires the old alert and raises a new one rather than quietly mutating the
old one's meaning.

## Rules decide, code does not

Every threshold comes from an `AlertRule` row: seeded as a platform default with
`project_id IS NULL`, optionally overridden per project.

| Column | Purpose |
|---|---|
| `alert_type` + `project_id` | the identity of a rule |
| `name` | label for the admin screen — *"90-day renewal warning"* beats `contract_expiring` |
| `is_enabled` | off means the type is not evaluated at all |
| `severity` | the band raised alerts start at |
| `config` | type-specific thresholds (`window_days`, `risk_score_cutoff`, `clause_types` …) |
| `escalate_after_days` | alerts older than this are raised a severity |
| `notify_channels` | in-app is always on; email and webhook are opt-in |

**Disabling a rule deliberately leaves its existing alerts alone** rather than
mass-resolving them: *"turning a rule off is a statement about the future, and silently
clearing an operator's queue is not what they asked for."*

## Lifecycle

```text
open ──► acknowledged ──► resolved
  │                          ▲
  └──────► dismissed         │
                    (or retired automatically by the sweep)
```

An alert row carries `title`, `message`, `details`, `due_date`, `severity`, `status`,
`acknowledged_by` / `acknowledged_at`, `resolved_at`, a free-text `note`, and the
`rule_id` that produced it.

## Where it runs

Inside **every worker**, behind a Postgres advisory lock so exactly one performs it per
tick however many workers are running — not a separate scheduler container. The cadence is
`ALERT_EVALUATOR_INTERVAL_MINUTES`, much slower than the other maintenance sweeps:
reclamation has to be prompt because a stalled job blocks a user, while alert conditions
move by the calendar and re-deriving every contract's deadlines once a minute would be a
full-table scan per minute to reach the same answer.

Run one pass by hand with `python -m app.cli evaluate-alerts` — useful after importing
contracts, or to see what a threshold change would do before leaving it in place.

**API:** `GET /alerts` · `PATCH /alerts/{id}` (acknowledge, resolve, dismiss) ·
`GET|POST|PATCH|DELETE /alerts/rules`.

---

# Part 3 — How the two connect

Both read the rows that stage **[4] EXTRACTION** produced:

```text
Contract ──► [4] EXTRACTION ──► obligations · key_dates · risks · entities
                                      │   (each row: page, bbox, confidence,
                                      │    parser / prompt / model version)
                                      │
                    ┌─────────────────┼─────────────────┬──────────────────┐
                    ▼                 ▼                 ▼                  ▼
              Portfolio         Contract detail     Dashboard        Alert evaluator
           (cross-contract,     (one document)      (aggregates)     (time-based sweep)
            scoped registers)
                    │                                                      │
                 PULL — go and look                                  PUSH — be told
```

The same `due_date` / `due_description` split drives both: Portfolio shows the undated
obligation in a review queue, and the alert evaluator can only raise `obligation_due` for
the ones that *have* a date. Converting the review queue into real dates is what turns a
row from something you must remember to look at into something that will find you.

---

## Source references

| Concern | Where |
|---|---|
| Registers, scope, ordering | `backend/app/api/v1/portfolio.py` |
| Response fields | `backend/app/schemas/portfolio.py` |
| Obligation / KeyDate / Risk / Entity models | `backend/app/models/knowledge.py` |
| UI tabs, filters, chips | `frontend/src/pages/PortfolioPage.tsx` |
| Alert evaluation and the three-step sweep | `backend/app/services/alert_evaluator.py` |
| Platform alerts raised in-path | `backend/app/services/alerts.py` |
| Alert and AlertRule models | `backend/app/models/alert.py` |
| Seeded default rules | `backend/app/db/seed.py` |
| Alert API | `backend/app/api/v1/admin.py` |
| Where the sweep runs | `backend/app/orchestrator/maintenance.py` |
