"""Retrieval Planner - the decision layer (§15).

Decides **what to retrieve and how** before anything is retrieved. It reads the
question, classifies its intent, picks one of the six strategies, resolves the
scope, and extracts the metadata filters the question implies. It executes nothing.

Why this is a separate layer rather than logic inside the search endpoint:

* **Metadata-first is a rule, not an optimisation (§0).** "Which contracts expire
  next quarter" is a date range over ``contract_metadata``, not a vector search.
  Answering it with an ANN scan is slower *and* wrong - similarity has no opinion
  about dates. The planner is where that decision is made once, rather than being
  re-litigated in every endpoint.
* **Hierarchical retrieval needs a plan.** Descending L1 → L2 → L3 only pays off if
  something decided in advance how many candidates each level should yield.
* **Scope is a security decision.** ``project_ids`` on the plan is the set of
  projects the caller is a member of, resolved once and carried through every
  subsequent query. Cross-project retrieval is prohibited (§1.1), so an empty scope
  must produce an empty result rather than an unscoped scan.

Planning is deterministic by default: rules over the question text, with an optional
LLM pass only for genuinely ambiguous queries. A deterministic plan is reproducible,
free, and fast - and the rules cover the overwhelming majority of real questions.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from app.core.config import get_settings
from app.core.enums import (
    EmbeddingLevel,
    QueryIntent,
    RetrievalStrategy,
    SearchMode,
    SearchScope,
)
from app.core.logging import get_logger

logger = get_logger(__name__)

#: Intent detection. Ordered: the first intent whose pattern matches wins, so the
#: more specific intents are listed before the general ones.
_INTENT_PATTERNS: tuple[tuple[QueryIntent, tuple[str, ...]], ...] = (
    (
        QueryIntent.COMPARISON,
        (
            r"\bcompare\b",
            r"\bversus\b",
            r"\bvs\.?\b",
            r"\bdifference between\b",
            r"\bdiffer\b",
            r"\bwhich (?:one|contract) (?:is|has) (?:better|more|less)\b",
        ),
    ),
    (
        QueryIntent.TIMELINE,
        (
            r"\bexpir\w*\b",
            r"\brenew\w*\b",
            r"\bdeadline\b",
            r"\bwhen (?:does|do|will|is)\b",
            r"\bnext (?:quarter|month|year|\d+ days)\b",
            r"\bupcoming\b",
            r"\btimeline\b",
            r"\bdue (?:date|by)\b",
            r"\bnotice period\b",
            r"\bcoming up\b",
        ),
    ),
    (
        QueryIntent.RISK_ASSESSMENT,
        (
            r"\brisk\w*\b",
            r"\bexposure\b",
            r"\bunlimited liability\b",
            r"\buncapped\b",
            r"\bdangerous\b",
            r"\bconcern\w*\b",
            r"\bred flag\b",
            r"\bproblematic\b",
        ),
    ),
    (
        QueryIntent.OBLIGATION_LOOKUP,
        (
            r"\bobligation\w*\b",
            r"\bwhat must\b",
            r"\bwho (?:is )?responsible\b",
            r"\brequired to\b",
            r"\bdeliverable\w*\b",
            r"\bwe (?:have|need) to\b",
            r"\bcommitment\w*\b",
        ),
    ),
    (
        QueryIntent.FINANCIAL,
        (
            r"\bfee\w*\b",
            r"\bprice\w*\b",
            r"\bpayment\b",
            r"\bcost\w*\b",
            r"\bvalue\b",
            r"\binvoice\b",
            r"\bhow much\b",
            r"\bspend\w*\b",
            r"\bbudget\b",
        ),
    ),
    (
        QueryIntent.COMPLIANCE,
        (
            r"\bcomply\w*\b",
            r"\bcompliance\b",
            r"\bgdpr\b",
            r"\bregulat\w+\b",
            r"\bpolicy\b",
            r"\bmandatory\b",
            r"\bmissing clause\w*\b",
            r"\baudit\b",
        ),
    ),
    (
        QueryIntent.RELATIONSHIP,
        (
            r"\brelated to\b",
            r"\bconnect\w+\b",
            r"\breference\w*\b",
            r"\bdepend\w+\b",
            r"\blinked\b",
            r"\bamend\w*\b",
            r"\bsupersede\w*\b",
            r"\bschedule\b",
        ),
    ),
    (
        QueryIntent.SUMMARIZATION,
        (
            r"\bsummar\w+\b",
            r"\boverview\b",
            r"\bexplain\b",
            r"\bwhat (?:is|are) this\b",
            r"\bkey (?:points|terms)\b",
            r"\btell me about\b",
            r"\bbrief\w*\b",
        ),
    ),
    (
        QueryIntent.CLAUSE_LOOKUP,
        (
            r"\bclause\w*\b",
            r"\bsection \d",
            r"\bprovision\w*\b",
            r"\bterm\w*\b",
            r"\bindemnit\w+\b",
            r"\bliabilit\w+\b",
            r"\bterminat\w+\b",
            r"\bconfidential\w*\b",
            r"\bgoverning law\b",
            r"\bwarrant\w+\b",
            r"\bexclusiv\w+\b",
            r"\bassign\w+\b",
        ),
    ),
    (
        QueryIntent.METADATA_LOOKUP,
        (
            r"\bhow many\b",
            r"\blist (?:all|the|every)\b",
            r"\bshow me (?:all|every)\b",
            r"\bcount\b",
            r"\bwhich contracts\b",
            r"\ball contracts\b",
            r"\bfilter\b",
        ),
    ),
)

#: Intent -> strategy. The mapping the frozen §15 table describes.
_INTENT_STRATEGY: dict[QueryIntent, RetrievalStrategy] = {
    QueryIntent.METADATA_LOOKUP: RetrievalStrategy.METADATA_ONLY,
    QueryIntent.CLAUSE_LOOKUP: RetrievalStrategy.CLAUSE_RETRIEVAL,
    QueryIntent.OBLIGATION_LOOKUP: RetrievalStrategy.CLAUSE_RETRIEVAL,
    QueryIntent.RISK_ASSESSMENT: RetrievalStrategy.METADATA_PLUS_DOCUMENT,
    QueryIntent.COMPARISON: RetrievalStrategy.CLAUSE_RETRIEVAL,
    QueryIntent.SUMMARIZATION: RetrievalStrategy.METADATA_PLUS_DOCUMENT,
    QueryIntent.TIMELINE: RetrievalStrategy.METADATA_ONLY,
    QueryIntent.RELATIONSHIP: RetrievalStrategy.GRAPH_TRAVERSAL,
    QueryIntent.COMPLIANCE: RetrievalStrategy.METADATA_PLUS_DOCUMENT,
    QueryIntent.FINANCIAL: RetrievalStrategy.HYBRID,
    QueryIntent.GENERAL_QA: RetrievalStrategy.HYBRID,
}

#: Clause vocabulary -> Clause Master key. Lets "what's the cap?" filter to the
#: liability clause before any vector is touched.
_CLAUSE_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "limitation_of_liability",
        (
            "liability",
            "liabilities",
            "cap",
            "capped",
            "uncapped",
            "consequential damages",
            "limitation of liability",
        ),
    ),
    (
        "indemnification",
        ("indemnity", "indemnities", "indemnify", "indemnification", "hold harmless"),
    ),
    (
        "intellectual_property",
        ("ip", "intellectual property", "work product", "copyright", "patent", "ownership of work"),
    ),
    ("payment_terms", ("payment", "invoice", "net 30", "fees", "pay", "payable")),
    ("term", ("term", "duration", "how long", "commencement")),
    ("auto_renewal", ("renewal", "renew", "auto-renew", "evergreen")),
    ("governing_law", ("governing law", "jurisdiction", "applicable law", "venue")),
    ("dispute_resolution", ("arbitration", "dispute", "litigation", "mediation", "seat")),
    ("confidentiality", ("confidential", "nda", "non-disclosure", "secrecy")),
    (
        "termination_for_convenience",
        ("terminate for convenience", "termination for convenience", "walk away", "exit"),
    ),
    (
        "termination_for_cause",
        ("terminate for cause", "termination for cause", "breach", "cure period"),
    ),
    ("force_majeure", ("force majeure", "act of god", "pandemic", "epidemic")),
    ("non_solicitation", ("non-solicit", "non solicit", "poach", "solicitation")),
    ("exclusivity", ("exclusive", "exclusivity")),
    ("assignment", ("assign", "assignment", "change of control", "novation")),
    ("audit_rights", ("audit", "inspect", "records")),
    ("insurance", ("insurance", "coverage", "insured")),
    ("data_protection", ("gdpr", "data protection", "personal data", "privacy", "dpa")),
    ("warranty", ("warranty", "warranties", "represent")),
    ("liquidated_damages", ("liquidated damages", "penalty", "penalties")),
    ("license_grant", ("licence", "license", "grant of rights")),
    ("survival", ("survive", "survival", "surviving")),
    ("notice", ("notice", "notify", "notification")),
)

#: Relative time expressions the planner resolves into an absolute window, so the
#: metadata filter is a real date range rather than a string the SQL cannot use.
_PERIOD_PATTERNS: tuple[tuple[str, int], ...] = (
    (r"\bnext (\d+) days?\b", 1),
    (r"\bnext (\d+) months?\b", 30),
    (r"\bnext (\d+) weeks?\b", 7),
    (r"\bnext quarter\b", 90),
    (r"\bnext month\b", 30),
    (r"\bnext year\b", 365),
    (r"\bthis quarter\b", 90),
    (r"\bthis month\b", 30),
    (r"\bthis year\b", 365),
    (r"\bwithin (\d+) days?\b", 1),
    (r"\bcoming (\d+) months?\b", 30),
)

_RISK_BANDS = ("high", "medium", "low")

#: A question naming a section or clause number is asking about that clause.
_CLAUSE_NUMBER = re.compile(
    r"\b(?:clause|section|article)\s+(?P<number>\d+(?:\.\d+)*)\b", re.IGNORECASE
)


@dataclass(slots=True)
class MetadataFilter:
    """Filters resolved from the question, applied *before* any vector scan.

    This is the metadata-first principle made concrete: narrowing to twelve
    contracts by agreement type and date, then searching those, beats searching
    everything and hoping similarity sorts it out.
    """

    agreement_types: list[str] = field(default_factory=list)
    risk_bands: list[str] = field(default_factory=list)
    clause_types: list[str] = field(default_factory=list)
    parties: list[str] = field(default_factory=list)
    expiring_before: date | None = None
    expiring_after: date | None = None
    effective_after: date | None = None
    has_unlimited_liability: bool | None = None
    missing_mandatory: bool | None = None
    contract_ids: list[uuid.UUID] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not any(
            [
                self.agreement_types,
                self.risk_bands,
                self.clause_types,
                self.parties,
                self.expiring_before,
                self.expiring_after,
                self.effective_after,
                self.has_unlimited_liability is not None,
                self.missing_mandatory is not None,
                self.contract_ids,
            ]
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "agreement_types": self.agreement_types,
            "risk_bands": self.risk_bands,
            "clause_types": self.clause_types,
            "parties": self.parties,
            "expiring_before": self.expiring_before.isoformat() if self.expiring_before else None,
            "expiring_after": self.expiring_after.isoformat() if self.expiring_after else None,
            "effective_after": self.effective_after.isoformat() if self.effective_after else None,
            "has_unlimited_liability": self.has_unlimited_liability,
            "missing_mandatory": self.missing_mandatory,
            "contract_ids": [str(cid) for cid in self.contract_ids],
        }

    def vector_metadata(self) -> dict[str, Any]:
        """The subset that can be pushed into the vector row's ``filter_metadata``.

        Only single-valued equality filters: JSONB containment cannot express "one
        of these three types" or a date range, and pretending otherwise would
        silently drop rows that should have matched.
        """
        payload: dict[str, Any] = {}
        if len(self.agreement_types) == 1:
            payload["agreement_type"] = self.agreement_types[0]
        if len(self.risk_bands) == 1:
            payload["risk_band"] = self.risk_bands[0]
        if self.has_unlimited_liability is not None:
            payload["has_unlimited_liability"] = self.has_unlimited_liability
        return payload


@dataclass(slots=True)
class LevelBudget:
    """How many candidates to take from one level of the hierarchy."""

    level: EmbeddingLevel
    limit: int
    min_similarity: float


@dataclass(slots=True)
class RetrievalPlan:
    """The decision. Executed by the retrieval engine, never by the planner."""

    query: str
    intent: QueryIntent
    strategy: RetrievalStrategy
    scope: SearchScope
    mode: SearchMode
    #: The projects this caller may read. The isolation boundary, resolved once and
    #: carried through every query the plan drives (§1.1).
    project_ids: list[uuid.UUID] = field(default_factory=list)
    contract_ids: list[uuid.UUID] = field(default_factory=list)
    filters: MetadataFilter = field(default_factory=MetadataFilter)
    levels: list[LevelBudget] = field(default_factory=list)
    #: Widen each retrieved chunk with its neighbours, so a clause that says "subject
    #: to the foregoing" arrives with the foregoing.
    neighbour_window: int = 1
    #: Walk the knowledge graph from retrieved nodes.
    graph_depth: int = 0
    rerank: bool = False
    #: How the plan was reached, for the "why did I get this answer" panel.
    reasoning: list[str] = field(default_factory=list)
    method: str = "rules"

    @property
    def is_scoped(self) -> bool:
        """False when no project is accessible - the engine must return nothing."""
        return bool(self.project_ids)

    def level_budget(self, level: EmbeddingLevel) -> LevelBudget | None:
        return next((entry for entry in self.levels if entry.level is level), None)

    def as_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "intent": self.intent.value,
            "strategy": self.strategy.value,
            "scope": self.scope.value,
            "mode": self.mode.value,
            "project_ids": [str(pid) for pid in self.project_ids],
            "contract_ids": [str(cid) for cid in self.contract_ids],
            "filters": self.filters.as_dict(),
            "levels": [
                {
                    "level": entry.level.value,
                    "limit": entry.limit,
                    "min_similarity": entry.min_similarity,
                }
                for entry in self.levels
            ],
            "neighbour_window": self.neighbour_window,
            "graph_depth": self.graph_depth,
            "rerank": self.rerank,
            "reasoning": self.reasoning,
            "method": self.method,
        }


class RetrievalPlanner:
    """Turns a question into a plan.

    Stateless. Deterministic unless an LLM tie-break is explicitly requested, so the
    same question against the same scope always produces the same plan - which is
    what makes a cached answer safe to reuse and a support ticket reproducible.
    """

    def __init__(self, *, now: datetime | None = None) -> None:
        self._settings = get_settings().retrieval
        # Injectable so a test can pin relative date resolution.
        self._now = now or datetime.now(UTC)

    def plan(
        self,
        query: str,
        *,
        project_ids: list[uuid.UUID],
        scope: SearchScope = SearchScope.PROJECT,
        contract_ids: list[uuid.UUID] | None = None,
        mode: SearchMode = SearchMode.HYBRID,
        agreement_types: list[str] | None = None,
    ) -> RetrievalPlan:
        text = (query or "").strip()
        reasoning: list[str] = []

        intent = self._detect_intent(text, reasoning)
        strategy = self._select_strategy(intent, text, scope, contract_ids, reasoning)
        filters = self._extract_filters(text, agreement_types, contract_ids, reasoning)
        levels = self._level_budgets(strategy, scope, reasoning)

        plan = RetrievalPlan(
            query=text,
            intent=intent,
            strategy=strategy,
            scope=scope,
            mode=mode,
            project_ids=list(project_ids),
            contract_ids=list(contract_ids or []),
            filters=filters,
            levels=levels,
            neighbour_window=self._neighbour_window(intent),
            graph_depth=(
                self._settings.graph_max_depth
                if strategy is RetrievalStrategy.GRAPH_TRAVERSAL
                else 0
            ),
            rerank=self._settings.reranker_enabled
            and strategy is not RetrievalStrategy.METADATA_ONLY,
            reasoning=reasoning,
        )

        if not plan.is_scoped:
            # Not an error: a user with no project membership legitimately sees
            # nothing. It is recorded so the empty result is explainable rather than
            # looking like a broken search.
            plan.reasoning.append("No accessible project, so this query can return no results.")

        logger.info(
            "retrieval_planned",
            intent=intent.value,
            strategy=strategy.value,
            scope=scope.value,
            projects=len(plan.project_ids),
            contracts=len(plan.contract_ids),
            filters=filters.as_dict() if not filters.is_empty else None,
            levels=[entry.level.value for entry in levels],
        )
        return plan

    # =========================================================================
    # Intent
    # =========================================================================
    def _detect_intent(self, query: str, reasoning: list[str]) -> QueryIntent:
        lowered = query.lower()
        for intent, patterns in _INTENT_PATTERNS:
            for pattern in patterns:
                if re.search(pattern, lowered):
                    reasoning.append(f"Intent '{intent.value}' - the question matches /{pattern}/.")
                    return intent
        reasoning.append("No specific intent matched; treating as general question answering.")
        return QueryIntent.GENERAL_QA

    def _select_strategy(
        self,
        intent: QueryIntent,
        query: str,
        scope: SearchScope,
        contract_ids: list[uuid.UUID] | None,
        reasoning: list[str],
    ) -> RetrievalStrategy:
        strategy = _INTENT_STRATEGY.get(intent, RetrievalStrategy.HYBRID)

        # A question naming a clause number wants that clause, whatever the intent
        # classifier thought - "what does section 11.2 say" is a lookup, not a summary.
        if _CLAUSE_NUMBER.search(query):
            reasoning.append(
                "The question names a specific clause number, so clause retrieval is used."
            )
            return RetrievalStrategy.CLAUSE_RETRIEVAL

        # Within a single contract, a metadata-only plan would answer from the
        # projection and never open the document - which is not what someone asking
        # about one contract wants.
        if scope is SearchScope.CONTRACT and strategy is RetrievalStrategy.METADATA_ONLY:
            reasoning.append(
                "Scoped to one contract, so document content is retrieved rather than "
                "answering from the metadata projection alone."
            )
            return RetrievalStrategy.HYBRID

        if contract_ids and strategy is RetrievalStrategy.METADATA_ONLY:
            reasoning.append("Specific contracts were named, so their content is retrieved.")
            return RetrievalStrategy.HYBRID

        reasoning.append(f"Strategy '{strategy.value}' follows from intent '{intent.value}'.")
        return strategy

    # =========================================================================
    # Filters
    # =========================================================================
    def _extract_filters(
        self,
        query: str,
        agreement_types: list[str] | None,
        contract_ids: list[uuid.UUID] | None,
        reasoning: list[str],
    ) -> MetadataFilter:
        lowered = query.lower()
        filters = MetadataFilter(
            agreement_types=list(agreement_types or []),
            contract_ids=list(contract_ids or []),
        )

        # --- clause vocabulary ------------------------------------------------
        for clause_key, terms in _CLAUSE_HINTS:
            if any(term in lowered for term in terms):
                filters.clause_types.append(clause_key)
        if filters.clause_types:
            reasoning.append("Clause filter: " + ", ".join(filters.clause_types[:5]) + ".")

        # --- risk band --------------------------------------------------------
        for band in _RISK_BANDS:
            if re.search(rf"\b{band}[- ]risk\b", lowered) or re.search(
                rf"\brisk (?:is |band )?{band}\b", lowered
            ):
                filters.risk_bands.append(band)
        if filters.risk_bands:
            reasoning.append("Risk band filter: " + ", ".join(filters.risk_bands) + ".")

        # --- exposure ---------------------------------------------------------
        if re.search(r"\bunlimited liabilit\w*\b|\buncapped\b", lowered):
            filters.has_unlimited_liability = True
            reasoning.append("Restricted to contracts with unlimited liability exposure.")
        if re.search(r"\bmissing clause\w*\b|\bmissing mandatory\b|\bincomplete\b", lowered):
            filters.missing_mandatory = True
            reasoning.append("Restricted to contracts missing a mandatory clause.")

        # --- time window ------------------------------------------------------
        window = self._resolve_period(lowered)
        if window is not None:
            today = self._now.date()
            filters.expiring_after = today
            filters.expiring_before = today + timedelta(days=window)
            reasoning.append(
                f"Date window: expiring between {filters.expiring_after} and "
                f"{filters.expiring_before} ({window} days)."
            )

        return filters

    def _resolve_period(self, lowered: str) -> int | None:
        """Turn "next quarter" or "within 60 days" into a number of days.

        Resolved here so the filter reaching SQL is an absolute range. Passing the
        phrase downstream would leave every consumer to reinterpret it, and they
        would not agree.
        """
        for pattern, multiplier in _PERIOD_PATTERNS:
            match = re.search(pattern, lowered)
            if not match:
                continue
            if match.groups():
                try:
                    return int(match.group(1)) * multiplier
                except (ValueError, IndexError):
                    continue
            return multiplier
        return None

    # =========================================================================
    # Budgets
    # =========================================================================
    def _level_budgets(
        self, strategy: RetrievalStrategy, scope: SearchScope, reasoning: list[str]
    ) -> list[LevelBudget]:
        """How many candidates each level yields.

        The hierarchy exists to keep the expensive level small: L1 narrows to
        candidate documents, L2 to candidate clauses within them, and only then does
        L3 pull evidence. Application-wide scope widens L1 because the candidate pool
        is larger, not L3 - the answer still only needs a handful of passages.
        """
        settings = self._settings
        wide = scope is SearchScope.APPLICATION
        budgets: list[LevelBudget] = []

        if strategy is RetrievalStrategy.METADATA_ONLY:
            reasoning.append("Metadata only: no vector search is performed.")
            return budgets

        if strategy in {
            RetrievalStrategy.METADATA_PLUS_DOCUMENT,
            RetrievalStrategy.HYBRID,
            RetrievalStrategy.GRAPH_TRAVERSAL,
        }:
            budgets.append(
                LevelBudget(
                    level=EmbeddingLevel.DOCUMENT_SUMMARY,
                    limit=settings.max_documents * (2 if wide else 1),
                    # Per-level floor: a document summary is long and topical, so
                    # its neighbours cluster differently from a short, formulaic
                    # clause. One global threshold suits neither.
                    min_similarity=settings.similarity_floor(EmbeddingLevel.DOCUMENT_SUMMARY.value),
                )
            )

        if strategy in {
            RetrievalStrategy.CLAUSE_RETRIEVAL,
            RetrievalStrategy.HYBRID,
            RetrievalStrategy.METADATA_PLUS_DOCUMENT,
            RetrievalStrategy.GRAPH_TRAVERSAL,
        }:
            budgets.append(
                LevelBudget(
                    level=EmbeddingLevel.CLAUSE,
                    limit=settings.max_clauses,
                    min_similarity=settings.similarity_floor(EmbeddingLevel.CLAUSE.value),
                )
            )

        if strategy in {RetrievalStrategy.CHUNK_RETRIEVAL, RetrievalStrategy.HYBRID}:
            budgets.append(
                LevelBudget(
                    level=EmbeddingLevel.CHUNK,
                    limit=settings.max_chunks,
                    min_similarity=settings.similarity_floor(EmbeddingLevel.CHUNK.value),
                )
            )

        reasoning.append(
            "Levels: " + ", ".join(f"{b.level.value}({b.limit})" for b in budgets) + "."
        )
        return budgets

    @staticmethod
    def _neighbour_window(intent: QueryIntent) -> int:
        """How far to widen a retrieved chunk.

        Wider for summarisation and comparison, where a passage read in isolation
        misleads; narrower for a lookup, where precision matters more than context
        and extra neighbours only dilute the answer.
        """
        if intent in {QueryIntent.SUMMARIZATION, QueryIntent.COMPARISON}:
            return 2
        if intent in {QueryIntent.METADATA_LOOKUP, QueryIntent.TIMELINE}:
            return 0
        return 1


__all__ = [
    "LevelBudget",
    "MetadataFilter",
    "RetrievalPlan",
    "RetrievalPlanner",
]
