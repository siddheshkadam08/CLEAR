"""Cross-contract views of extracted knowledge.

Extraction has always produced obligations, key dates, risks and parties, and
:mod:`app.api.v1.knowledge` has always served them - one contract at a time. That
answers "what is in this agreement?" and nothing else. It cannot answer "what is
due this month?", "which counterparties do we have the most exposure to?" or
"where are the unlimited liability clauses?", which are the questions a contract
repository exists to answer.

These schemas are the per-contract ones plus the context a cross-contract list
needs: which contract a row came from, and what it is called. Without the title a
register is a page of duties attached to UUIDs.

They deliberately do **not** re-serve evidence coordinates. A portfolio row is a
pointer - the reader follows it to the contract, where the highlight lives. Sending
bounding boxes for a 200-row register would multiply the payload for something no
list view can draw.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from pydantic import Field

from app.core.enums import DateType, EntityType, ObligationStatus, RiskSeverity
from app.schemas.common import ResponseSchema


class PortfolioItem(ResponseSchema):
    """Where a row came from. Every portfolio row carries this."""

    contract_id: uuid.UUID
    project_id: uuid.UUID
    #: Denormalised from the contract so a register renders in one query. Null only
    #: if the contract has no title yet - extraction not finished, or a document
    #: whose title the parser could not find.
    contract_title: str | None = None
    contract_number: str | None = None


class PortfolioObligation(PortfolioItem):
    """One duty, in the cross-contract register."""

    id: uuid.UUID
    action: str
    responsible_party: str | None = None
    due_date: date | None = None
    #: Kept when the contract expresses the deadline relatively ("within 30 days of
    #: invoice"). That wording *is* the obligation; a computed date would lose it,
    #: which is why an undated obligation is listed rather than dropped.
    due_description: str | None = None
    trigger_event: str | None = None
    frequency: str | None = None
    is_recurring: bool = False
    status: ObligationStatus = ObligationStatus.OPEN
    penalty: str | None = None
    clause_id: uuid.UUID | None = None


class PortfolioKeyDate(PortfolioItem):
    """One dated milestone, in the cross-contract timeline."""

    id: uuid.UUID
    date_type: DateType
    date_value: date | None = None
    #: A relative expression extraction could not resolve to a calendar date. It is
    #: preserved verbatim rather than discarded or guessed at.
    date_expression: str | None = None
    description: str | None = None
    is_recurring: bool = False


class PortfolioRisk(PortfolioItem):
    """One finding, in the cross-contract risk register."""

    id: uuid.UUID
    risk_type: str
    severity: RiskSeverity
    description: str
    recommendation: str | None = None
    category: str | None = None
    score_contribution: int | None = None
    #: True when the risk is the *absence* of something - no liability cap, no
    #: data-protection clause. An omission has no clause text to point at.
    is_omission: bool = False
    clause_id: uuid.UUID | None = None
    #: The owning contract's overall 0-100 score, so a row can be read against the
    #: agreement it sits in rather than in isolation.
    contract_risk_score: int | None = None


class PartyDirectoryEntry(ResponseSchema):
    """One counterparty, aggregated across every contract that names them.

    **Grouped by exact name, case-insensitively.** Not by entity resolution:
    "Acme Corp", "Acme Corporation Inc." and "ACME" would be one counterparty to a
    lawyer and are three rows here. Merging them needs the trigram index on
    ``entities.name`` and a human-confirmable match, and a directory that guessed
    would under-report exposure - the one number this screen exists to give.
    """

    #: The lower-cased name the grouping keyed on. Stable enough to link with.
    key: str
    #: The most complete spelling seen - a registered legal name where one contract
    #: gave one, otherwise the name as extracted.
    name: str
    entity_types: list[EntityType] = Field(default_factory=list)
    roles: list[str] = Field(default_factory=list)
    jurisdictions: list[str] = Field(default_factory=list)
    contract_count: int = 0
    #: True where at least one contract marks them a primary signatory, which is
    #: what separates a counterparty from a name mentioned in passing.
    is_primary_anywhere: bool = False
    #: Total value of the contracts they appear in, where a value was extracted.
    #: Currencies are reported separately rather than summed across them.
    total_value: dict[str, float] = Field(default_factory=dict)
    #: The most recent expiry among their contracts, for "who is up for renewal".
    next_expiry: date | None = None
    sample_contract_id: uuid.UUID | None = None


class PortfolioSummary(ResponseSchema):
    """Counts behind the register, for the filter bar.

    Returned alongside a page rather than derived from it: a page of 25 cannot say
    how many Critical risks the estate holds, and a filter chip showing "3" when
    the answer is 340 is worse than no number.
    """

    total: int = 0
    by_key: dict[str, int] = Field(default_factory=dict)
    extra: dict[str, Any] = Field(default_factory=dict)


__all__ = [
    "PartyDirectoryEntry",
    "PortfolioItem",
    "PortfolioKeyDate",
    "PortfolioObligation",
    "PortfolioRisk",
    "PortfolioSummary",
]
