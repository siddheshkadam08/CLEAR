"""Copilot query API contract.

camelCase, unlike every other schema in this package. That is deliberate and it
is the only reason this module exists separately from :mod:`app.schemas.search`:
this endpoint's field names are part of an agreed external contract, and quietly
renaming them to match the house snake_case style would break the callers the
contract was written for.

``populate_by_name`` is already set on the shared base, so the aliases here are
additive - the models still construct from Python-side names in tests and
services, and only the wire format changes.
"""

from __future__ import annotations

import uuid

from pydantic import ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel

from app.schemas.common import BaseSchema, ResponseSchema


class _CamelRequest(BaseSchema):
    model_config = ConfigDict(
        from_attributes=True,
        use_enum_values=True,
        populate_by_name=True,
        str_strip_whitespace=True,
        alias_generator=to_camel,
        extra="forbid",
    )


class _CamelResponse(ResponseSchema):
    model_config = ConfigDict(
        from_attributes=True,
        use_enum_values=True,
        populate_by_name=True,
        alias_generator=to_camel,
        # Responses stay permissive: adding a field must not be a breaking change
        # for a client that round-trips the payload.
        extra="ignore",
    )


class CopilotQueryRequest(_CamelRequest):
    """A question, scoped to a project and optionally to one contract."""

    query: str = Field(min_length=1, max_length=2000)
    #: Omitted means every project the caller belongs to - never the whole table
    #: (§1.1). The scope is resolved from membership regardless of what is sent.
    project_id: uuid.UUID | None = None
    #: Narrow to a single agreement. Verified against the caller's scope before
    #: use; one in another project is reported as missing, not as forbidden.
    contract_id: uuid.UUID | None = None
    #: Continue a conversation, so a follow-up's pronouns resolve.
    session_id: uuid.UUID | None = None

    @field_validator("query")
    @classmethod
    def _strip(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("The query cannot be empty.")
        return cleaned


class CopilotSource(_CamelResponse):
    """One passage the answer was built from."""

    contract_id: uuid.UUID
    contract_name: str | None = None
    clause_heading: str | None = None
    section_number: str | None = None
    page_number: int | None = None
    #: Cosine similarity against the query, in [0, 1]. Not the hybrid fusion
    #: score, which is on an unrelated scale and would be meaningless here.
    #:
    #: ``None`` for a keyword-only match, which has no similarity to report.
    #: Deliberately not defaulted to 0.0: that reads as "irrelevant" next to what
    #: may be the best exact-phrase match in the corpus.
    similarity_score: float | None = None
    #: The re-ranker's relevance judgement, when one ran. Reported separately
    #: rather than folded into ``similarity_score`` - they measure different things.
    rerank_score: float | None = None
    #: ``semantic`` · ``keyword`` · ``hybrid`` · ``context``. Explains a missing
    #: similarity rather than leaving it as an unexplained gap.
    match_type: str = "semantic"
    #: The passage itself, so a reader can check the claim without opening the PDF.
    text: str = ""
    #: The ``[n]`` marker this passage carries in the answer text.
    label: int = 0


class CopilotQueryMetadata(_CamelResponse):
    """How the answer was reached - shown when a result needs explaining."""

    #: True when a known document type actually narrowed the search. A type the
    #: classifier proposed and the threshold rejected is *not* detected: it
    #: narrowed nothing.
    document_type_detected: bool = False
    document_type: str | None = None
    document_type_confidence: float = 0.0
    #: ``DocumentTypeFiltered`` · ``ContractScoped`` · ``Unfiltered``
    retrieval_mode: str = "Unfiltered"
    #: Passages that reached the prompt, after re-ranking and the context budget.
    retrieved_chunks: int = 0
    #: Best cosine similarity anything retrieved achieved. Compared against the
    #: configured threshold to decide whether the question could be answered.
    top_similarity: float = 0.0
    #: True when the guardrail fired: nothing retrieved cleared the threshold, so
    #: the answer is the fixed message and no model was asked.
    insufficient_context: bool = False
    #: True when retrieval succeeded but the model could not be reached. The
    #: sources are still populated; the answer text is not a synthesis.
    generation_failed: bool = False
    #: True when a document-type filter matched nothing and the search was widened.
    #: A thin answer from a widened search reads exactly like a thin answer from a
    #: narrow one unless this is surfaced.
    relaxed_filters: bool = False
    #: True when more contracts matched than the pre-filter can carry, so results
    #: are ranked across the whole project rather than a pre-selected subset.
    scope_truncated: bool = False
    confidence: float = 0.0
    confidence_band: str = "low"
    #: True when the answer should be checked before it is relied on.
    needs_review: bool = False
    refused: bool = False
    warnings: list[str] = Field(default_factory=list)
    model: str | None = None
    tokens: int = 0
    cost_usd: float = 0.0
    #: ``{analysis_ms, retrieval_ms, rerank_ms, inference_ms, total_ms}``.
    timings: dict[str, int] = Field(default_factory=dict)


class CopilotQueryResponse(_CamelResponse):
    """A grounded answer with its sources.

    There is no separate ``explanation`` field. The answering prompt already
    instructs the model to lead with the answer and follow it with the supporting
    reasoning, so splitting the two would mean either asking for the same content
    twice or cutting a narrative apart on a heuristic - and a wrongly split answer
    reads as a contradiction between its own halves.
    """

    answer: str
    sources: list[CopilotSource] = Field(default_factory=list)
    metadata: CopilotQueryMetadata = Field(default_factory=CopilotQueryMetadata)
    session_id: uuid.UUID | None = None
    message_id: uuid.UUID | None = None


__all__ = [
    "CopilotQueryMetadata",
    "CopilotQueryRequest",
    "CopilotQueryResponse",
    "CopilotSource",
]
