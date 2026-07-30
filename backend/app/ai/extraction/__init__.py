"""AI extraction (§13).

Turns a chunked contract into structured, cited knowledge: clauses with typed
attributes, parties, obligations, key dates, risks, relationships and a 0-100 risk
score.

The pieces, in the order the engine uses them:

* :mod:`~app.ai.extraction.evidence` - deterministic pre-filter that decides which
  chunks a category is worth asking about, and reports honest absence when none is.
* :mod:`~app.ai.extraction.schemas` - the JSON Schema for every call. Free-form
  output is prohibited.
* :mod:`~app.ai.extraction.prompts` - the grounding rules and the cached prefix.
* :mod:`~app.ai.extraction.validation` - schema conformance, citation and quote
  verification, and the business rules a schema cannot express.
* :mod:`~app.ai.extraction.risk` - deterministic, explainable risk scoring.
* :mod:`~app.ai.extraction.engine` - orchestration, cost accounting and review
  triggers.

Nothing here touches the database. The engine returns
:class:`~app.ai.extraction.models.ExtractionResult`; the ``ai_extraction`` stage
persists it.
"""

from app.ai.extraction.engine import (
    ClauseDefinition,
    ExtractionEngine,
    ExtractionRequest,
)
from app.ai.extraction.evidence import (
    CandidateChunk,
    EvidenceBundle,
    EvidenceSelector,
)
from app.ai.extraction.models import (
    CategoryOutcome,
    ContractFacts,
    EvidenceRef,
    ExtractedClause,
    ExtractedKeyDate,
    ExtractedObligation,
    ExtractedParty,
    ExtractedRelationship,
    ExtractedRisk,
    ExtractionResult,
    RiskAssessment,
    ValidationIssue,
)
from app.ai.extraction.prompts import GROUNDING_RULES, ExtractionPromptBuilder
from app.ai.extraction.risk import RiskAssessor

__all__ = [
    "GROUNDING_RULES",
    "CandidateChunk",
    "CategoryOutcome",
    "ClauseDefinition",
    "ContractFacts",
    "EvidenceBundle",
    "EvidenceRef",
    "EvidenceSelector",
    "ExtractedClause",
    "ExtractedKeyDate",
    "ExtractedObligation",
    "ExtractedParty",
    "ExtractedRelationship",
    "ExtractedRisk",
    "ExtractionEngine",
    "ExtractionPromptBuilder",
    "ExtractionRequest",
    "ExtractionResult",
    "RiskAssessment",
    "RiskAssessor",
    "ValidationIssue",
]
