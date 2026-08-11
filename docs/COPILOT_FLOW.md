# Copilot — how a question becomes a grounded answer

What happens between a user typing a question and an answer appearing with citations.
Validated against the implementation; the source references at the bottom let you check
any claim. Where this differs from *Flow & Architecture Diagrams v1.2, Figure 3 (Semantic
Query Pipeline)*, the differences are listed in **What this corrects**.

---

## The flow

```text
User question
   POST /copilot/query          (JSON answer)
   POST /copilot/stream         (SSE, token by token — same pipeline, different transport)
         │
         ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ [0] SCOPE — resolved before anything else runs                              │
│                                                                             │
│ Which projects and which contract this question may see, from the caller's   │
│ own memberships. Re-asserted on the contract id: a caller cannot widen its   │
│ own scope by naming a contract in another project.                          │
│                                                                             │
│ An unscoped plan returns nothing rather than everything — the fail-safe      │
│ direction, because the alternative is a cross-project leak.                 │
└─────────────────────────────────────────────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ [1] QUERY ANALYSIS                          app/ai/retrieval/analysis.py    │
│                                                                             │
│ One LLM call classifies the question:                                       │
│   • intent          • documentType (or null)                                │
│   • confidence      • reasoning (the words that decided it)                 │
│                                                                             │
│ The model is told three times that it must classify and never answer — it    │
│ has been shown no contract, so anything it said about one would be invented. │
│ Cached for an hour, keyed by a digest of the question.                      │
│                                                                             │
│ A low-confidence document type is not used as a filter: a confident guess    │
│ excludes the right contract from the search entirely.                       │
└─────────────────────────────────────────────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ [2] PLAN → RETRIEVE                app/ai/retrieval/planner.py, engine.py   │
│                                                                             │
│ The planner decides what to search, how deep, and within which projects.     │
│ The engine makes no decisions — it executes. Order is the hierarchy, and it  │
│ is not optional:                                                            │
│                                                                             │
│   1. METADATA PRE-FILTER   indexed lookup; shrinks everything after it       │
│   2. L1 DOCUMENT SUMMARY   rank candidate *documents*                        │
│                            (skipped when the plan already names contracts)   │
│   3. L2 CLAUSE / L3 CHUNK  search only within the survivors                  │
│   4. HYBRID FUSION         vector + keyword, combined by reciprocal rank      │
│                            fusion, then optionally re-ranked                 │
│                            (RERANKER_ENABLED defaults to false)              │
│   5. EXPANSION             widen each hit with its neighbours and parents,    │
│                            so a clause arrives with the context it needs      │
└─────────────────────────────────────────────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ [3] RELAXED RETRY — one bounded second attempt                              │
│                                                                             │
│ If nothing came back AND a document-type filter was applied, the search is   │
│ repeated once without it.                                                   │
│                                                                             │
│ Why: "the classifier picked the wrong type" and "the answer is not in the    │
│ corpus" are indistinguishable from here, and only one of them is worth       │
│ telling the user about. The relaxation is recorded in the plan's reasoning   │
│ so the answer can say the search was widened.                               │
└─────────────────────────────────────────────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ [4] GUARDRAIL — the decision that makes this safe          ★                │
│                                                                             │
│   if answerable_similarity < COPILOT_SIMILARITY_THRESHOLD (0.45):           │
│       return a fixed sentence — AND MAKE NO INFERENCE CALL AT ALL           │
│                                                                             │
│ Not a cost optimisation, a correctness one. A model handed weak evidence     │
│ and a contract question will produce something plausible from its general    │
│ knowledge of what contracts usually say — which is the exact failure this    │
│ platform exists to prevent.                                                 │
│                                                                             │
│ `answerable_similarity`, NOT `top_similarity`. An L1 document summary is     │
│ long and topical and scores 0.55–0.70 against almost any question about that │
│ contract. Comparing the overall maximum against the threshold let a document │
│ that was merely *about* the subject vouch for clause evidence scoring 0.31 — │
│ the exact case this guardrail exists to catch.                              │
└─────────────────────────────────────────────────────────────────────────────┘
         │  (only if the bar is cleared)
         ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ [5] CONTEXT ASSEMBLY                       app/ai/retrieval/context.py      │
│                                                                             │
│ Evidence is packed to a token budget (CONTEXT_TOKEN_BUDGET), each passage    │
│ labelled [1], [2] … and carrying its contract, clause and page. Conversation │
│ history is included for multi-turn sessions.                                │
└─────────────────────────────────────────────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ [6] PROMPT ORCHESTRATION                    app/ai/rag/orchestrator.py      │
│                                                                             │
│ Decides what the model is asked and how the answer must be shaped. It does   │
│ not retrieve and does not call a model.                                     │
│                                                                             │
│ The grounding rules are answering-specific, not the extraction ones:         │
│   1. Answer only from the supplied evidence                                 │
│   2. Cite every factual claim by bracketed label                            │
│   3. Never invent contract content — no cleaner paraphrase, no merging      │
│   4. Distinguish "the contract is silent" from "I was not given that part"  │
│   5. State uncertainty; quote both sides where passages conflict            │
│                                                                             │
│ Layout follows prompt caching: the system prompt is identical per response   │
│ format; evidence, history and question go after the cache breakpoint.       │
└─────────────────────────────────────────────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ [7] GENERATE                                     app/ai/rag/engine.py       │
│                                                                             │
│ Consumes the package and the prompt. Never retrieves, never builds prompts.  │
└─────────────────────────────────────────────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ [8] ANSWER VALIDATION — what happens AFTER generation      ★                │
│                                                                             │
│ "A model asked to cite will usually cite; the question is whether the        │
│  citations are real."                                                       │
│                                                                             │
│   • FABRICATED CITATIONS ARE STRIPPED. A label the model was never offered   │
│     cannot resolve to a page, so it is removed rather than shown to a user   │
│     who would reasonably assume a bracketed number is verifiable.           │
│   • UNCITED FACTUAL ANSWERS ARE FLAGGED. An answer of substance with no      │
│     citations did not come from the evidence, whatever it says.             │
│   • CONFIDENCE REFLECTS GROUNDING, NOT FLUENCY — computed from citation      │
│     coverage and retrieval scores.                                          │
│   • A REFUSAL IS A FIRST-CLASS OUTCOME. Contract language around indemnities │
│     and breach sits close to safety categories, so the caller must be able   │
│     to tell "the model declined" from "the model failed".                   │
└─────────────────────────────────────────────────────────────────────────────┘
         │
         ▼
   Answer + citations (contract, clause, page) + confidence band
```

### Streaming

`POST /copilot/stream` runs the **same** pipeline — classification, document-type filter,
re-ranking, guardrail — so a question is never refused over one transport and answered
over the other. Only delivery differs.

**Citations are emitted after the text completes, not during.** A citation can only be
checked once the text containing it exists, and streaming an unverified label would put a
reference on screen that might then be withdrawn.

---

## What this corrects

Validated against *Flow & Architecture Diagrams v1.2*, Figure 3. The documented flow —
query analysis → embedding → vector search → top-K → optional rerank → prompt → answer —
is directionally right, and the scoping by project/contract/document-type is real. Six
differences:

| Diagram says | Implementation |
|---|---|
| **"A LangChain pipeline"** | **LangChain is not used.** Zero references in application code — the sequence is assembled explicitly in `app/services/copilot.py`. `langgraph`/`langchain-core` exist only as an optional, uninstalled extra |
| Vector search over pgvector, top-K | A **five-step hierarchy**: metadata pre-filter → L1 document → L2 clause / L3 chunk → **hybrid fusion of vector *and keyword* by reciprocal rank fusion** → **expansion** with neighbours and parents. Keyword search and expansion are absent from the diagram |
| — | **A relaxed retry.** If a document-type filter returned nothing, the search repeats once without it, because a misclassification and an empty corpus look identical from there |
| Retrieval → prompt → LLM | **A guardrail sits between them.** Below `COPILOT_SIMILARITY_THRESHOLD` (0.45) the answer is a fixed sentence and **no inference call is made at all**. This is the single most important safety property and the diagram has no box for it |
| "passed to an LLM to generate the final answer" | Generation is followed by **answer validation**: fabricated citations stripped, uncited answers flagged, confidence computed from grounding rather than fluency |
| "returned to the user with its supporting sources" | Also **streamed** over SSE, with citations deliberately emitted only after the text completes. And multi-turn **sessions** are supported (`/copilot/sessions`), which the diagram does not show |

None of these make the diagram wrong about *intent* — they make it incomplete about the
parts that stop a confident wrong answer reaching a lawyer.

---

## Configuration

| Setting | Default | Effect |
|---|---|---|
| `COPILOT_SIMILARITY_THRESHOLD` | `0.45` | Below this, refuse without calling the model |
| `COPILOT_DOCTYPE_CONFIDENCE_THRESHOLD` | `0.75` | Below this, the classifier's document type is not used as a filter |
| `RERANKER_ENABLED` | `false` | Cross-encoder re-rank after fusion |
| `RETRIEVAL_MAX_DOCUMENTS / _CLAUSES / _CHUNKS` | 25 / 40 / 20 | Per-level caps |
| `RETRIEVAL_MIN_SIMILARITY_DOCUMENT / _CLAUSE / _CHUNK` | 0.35 / 0.45 / 0.40 | Per-level floors — what is *returned*, distinct from the answerability guardrail |
| `CONTEXT_TOKEN_BUDGET` | `24000` | Ceiling for assembled evidence |

---

## Source references

| Concern | Where |
|---|---|
| Pipeline sequence, scope, guardrail | `backend/app/services/copilot.py` |
| Query classification | `backend/app/ai/retrieval/analysis.py` |
| Plan construction | `backend/app/ai/retrieval/planner.py` |
| Hierarchy, fusion, expansion | `backend/app/ai/retrieval/engine.py` |
| Context packing | `backend/app/ai/retrieval/context.py` |
| Grounding rules, prompt shape | `backend/app/ai/rag/orchestrator.py` |
| Generation + citation validation | `backend/app/ai/rag/engine.py` |
| HTTP surface, SSE streaming | `backend/app/api/v1/search.py` |
