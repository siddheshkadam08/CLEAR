"""Canonical enumerations shared by models, schemas, pipeline and API.

Single source of truth: SQLAlchemy native enums, Pydantic contracts and the
frontend TypeScript types are all generated from or validated against these.
Values are lowercase snake_case strings except :class:`JobState`, which is
uppercase to match the state machine in the specification.
"""

from __future__ import annotations

from enum import StrEnum


# =============================================================================
# Identity & access
# =============================================================================
class RoleName(StrEnum):
    """The four platform roles.

    UI labels map onto these: *Admin* → ``SYSTEM_ADMIN``, *Contract Manager* →
    ``PROJECT_MANAGER``, *Viewer* → ``VIEWER``. An SSO-authenticated user is not
    a separate role - they are granted one of these per project on invite.
    """

    SYSTEM_ADMIN = "system_admin"
    PROJECT_MANAGER = "project_manager"
    REVIEWER = "reviewer"
    VIEWER = "viewer"


class Permission(StrEnum):
    """Fine-grained permissions stored as JSONB on ``roles.permissions``.

    Checked by the ``require_permission(project_id, perm)`` dependency. System
    Admins bypass the check entirely.
    """

    # projects
    PROJECT_CREATE = "project:create"
    PROJECT_READ = "project:read"
    PROJECT_UPDATE = "project:update"
    PROJECT_DELETE = "project:delete"
    PROJECT_MEMBER_MANAGE = "project:member:manage"

    # contracts
    CONTRACT_UPLOAD = "contract:upload"
    CONTRACT_READ = "contract:read"
    CONTRACT_UPDATE = "contract:update"
    CONTRACT_DELETE = "contract:delete"
    CONTRACT_DOWNLOAD = "contract:download"

    # processing
    JOB_READ = "job:read"
    JOB_CONTROL = "job:control"  # retry / cancel / pause / resume

    # knowledge
    KNOWLEDGE_READ = "knowledge:read"
    KNOWLEDGE_REVIEW = "knowledge:review"  # accept/reject AI extraction

    # retrieval
    SEARCH_EXECUTE = "search:execute"
    COPILOT_USE = "copilot:use"

    # reporting
    REPORT_VIEW = "report:view"
    EXPORT_CREATE = "export:create"

    # administration
    USER_MANAGE = "user:manage"
    ROLE_MANAGE = "role:manage"
    CLAUSE_MASTER_MANAGE = "clause_master:manage"
    AI_SETTINGS_MANAGE = "ai_settings:manage"
    PROFILE_MANAGE = "profile:manage"
    AUDIT_READ = "audit:read"
    ALERT_RULE_MANAGE = "alert_rule:manage"


class AuthProvider(StrEnum):
    LOCAL = "local"
    MICROSOFT = "microsoft"


class ProjectStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"
    ON_HOLD = "on_hold"


# =============================================================================
# Documents & processing
# =============================================================================
class FileType(StrEnum):
    PDF = "pdf"
    DOCX = "docx"


class ContractStatus(StrEnum):
    """Lifecycle of the contract record itself (not its processing job)."""

    UPLOADED = "uploaded"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"
    NEEDS_REVIEW = "needs_review"
    ARCHIVED = "archived"


class JobState(StrEnum):
    """Processing job state machine (§10.1).

    ``QUEUED → VALIDATING → PARSING → ENRICHING → CLASSIFYING → CHUNKING
    → AI_EXTRACTION → EMBEDDING → INDEXING → READY``
    with ``FAILED · RETRYING · CANCELLED · PAUSED`` as alternates.
    """

    QUEUED = "QUEUED"
    VALIDATING = "VALIDATING"
    PARSING = "PARSING"
    ENRICHING = "ENRICHING"
    CLASSIFYING = "CLASSIFYING"
    CHUNKING = "CHUNKING"
    AI_EXTRACTION = "AI_EXTRACTION"
    EMBEDDING = "EMBEDDING"
    INDEXING = "INDEXING"
    READY = "READY"
    FAILED = "FAILED"
    RETRYING = "RETRYING"
    CANCELLED = "CANCELLED"
    PAUSED = "PAUSED"

    @property
    def is_terminal(self) -> bool:
        return self in {JobState.READY, JobState.FAILED, JobState.CANCELLED}

    @property
    def is_running(self) -> bool:
        return self in {
            JobState.VALIDATING,
            JobState.PARSING,
            JobState.ENRICHING,
            JobState.CLASSIFYING,
            JobState.CHUNKING,
            JobState.AI_EXTRACTION,
            JobState.EMBEDDING,
            JobState.INDEXING,
        }


class PipelineStage(StrEnum):
    """The eight pipeline stages. Order is significant - see ``STAGE_ORDER``."""

    VALIDATION = "validation"
    PARSER = "parser"
    ENRICHMENT = "enrichment"
    CLASSIFICATION = "classification"
    CHUNKING = "chunking"
    AI_EXTRACTION = "ai_extraction"
    EMBEDDING = "embedding"
    INDEXING = "indexing"


#: Canonical execution order. Index position drives resume-from-checkpoint and
#: "regenerate only downstream stages" logic.
STAGE_ORDER: tuple[PipelineStage, ...] = (
    PipelineStage.VALIDATION,
    PipelineStage.PARSER,
    PipelineStage.ENRICHMENT,
    PipelineStage.CLASSIFICATION,
    PipelineStage.CHUNKING,
    PipelineStage.AI_EXTRACTION,
    PipelineStage.EMBEDDING,
    PipelineStage.INDEXING,
)

#: Stage → the job state held while that stage runs.
STAGE_TO_STATE: dict[PipelineStage, JobState] = {
    PipelineStage.VALIDATION: JobState.VALIDATING,
    PipelineStage.PARSER: JobState.PARSING,
    PipelineStage.ENRICHMENT: JobState.ENRICHING,
    PipelineStage.CLASSIFICATION: JobState.CLASSIFYING,
    PipelineStage.CHUNKING: JobState.CHUNKING,
    PipelineStage.AI_EXTRACTION: JobState.AI_EXTRACTION,
    PipelineStage.EMBEDDING: JobState.EMBEDDING,
    PipelineStage.INDEXING: JobState.INDEXING,
}

#: Hard dependency graph validated by the Workflow Engine before dispatch.
STAGE_DEPENDENCIES: dict[PipelineStage, tuple[PipelineStage, ...]] = {
    PipelineStage.VALIDATION: (),
    PipelineStage.PARSER: (PipelineStage.VALIDATION,),
    PipelineStage.ENRICHMENT: (PipelineStage.PARSER,),
    PipelineStage.CLASSIFICATION: (PipelineStage.ENRICHMENT,),
    PipelineStage.CHUNKING: (PipelineStage.CLASSIFICATION,),
    PipelineStage.AI_EXTRACTION: (PipelineStage.CHUNKING,),
    PipelineStage.EMBEDDING: (PipelineStage.AI_EXTRACTION,),
    PipelineStage.INDEXING: (PipelineStage.EMBEDDING,),
}


def stage_index(stage: PipelineStage) -> int:
    return STAGE_ORDER.index(stage)


def stages_from(stage: PipelineStage) -> tuple[PipelineStage, ...]:
    """``stage`` and every stage after it - the replay set for a reprocess."""
    return STAGE_ORDER[stage_index(stage) :]


class StageStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"  # reused a valid checkpoint
    CANCELLED = "cancelled"


class JobPriority(StrEnum):
    HIGH = "high"
    NORMAL = "normal"
    LOW = "low"

    @property
    def queue_weight(self) -> int:
        """BullMQ priority: lower number = higher priority."""
        return {JobPriority.HIGH: 1, JobPriority.NORMAL: 5, JobPriority.LOW: 10}[self]


class ArtifactKind(StrEnum):
    """Every artifact a stage can emit (§7.3)."""

    VALIDATION = "validation"
    NORMALIZED_DOCUMENT = "normalized_document"
    CANONICAL_DOCUMENT = "canonical_document"
    CLASSIFICATION = "classification"
    CHUNKS = "chunks"
    CHUNK_STATISTICS = "chunk_statistics"
    CHUNK_VALIDATION = "chunk_validation"
    CLAUSES = "clauses"
    ENTITIES = "entities"
    OBLIGATIONS = "obligations"
    RISKS = "risks"
    TIMELINES = "timelines"
    RELATIONSHIPS = "relationships"
    EXTRACTION_STATISTICS = "extraction_statistics"
    SUMMARY_EMBEDDINGS = "summary_embeddings"
    CLAUSE_EMBEDDINGS = "clause_embeddings"
    CHUNK_EMBEDDINGS = "chunk_embeddings"
    EMBEDDING_STATISTICS = "embedding_statistics"
    INDEX_STATISTICS = "index_statistics"
    STATISTICS = "statistics"
    EXPORT = "export"


#: Which artifacts each stage produces. Used to invalidate downstream artifacts.
STAGE_ARTIFACTS: dict[PipelineStage, tuple[ArtifactKind, ...]] = {
    PipelineStage.VALIDATION: (ArtifactKind.VALIDATION,),
    PipelineStage.PARSER: (ArtifactKind.NORMALIZED_DOCUMENT,),
    PipelineStage.ENRICHMENT: (ArtifactKind.CANONICAL_DOCUMENT, ArtifactKind.STATISTICS),
    PipelineStage.CLASSIFICATION: (ArtifactKind.CLASSIFICATION,),
    PipelineStage.CHUNKING: (
        ArtifactKind.CHUNKS,
        ArtifactKind.CHUNK_STATISTICS,
        ArtifactKind.CHUNK_VALIDATION,
    ),
    PipelineStage.AI_EXTRACTION: (
        ArtifactKind.CLAUSES,
        ArtifactKind.ENTITIES,
        ArtifactKind.OBLIGATIONS,
        ArtifactKind.RISKS,
        ArtifactKind.TIMELINES,
        ArtifactKind.RELATIONSHIPS,
        ArtifactKind.EXTRACTION_STATISTICS,
    ),
    PipelineStage.EMBEDDING: (
        ArtifactKind.SUMMARY_EMBEDDINGS,
        ArtifactKind.CLAUSE_EMBEDDINGS,
        ArtifactKind.CHUNK_EMBEDDINGS,
        ArtifactKind.EMBEDDING_STATISTICS,
    ),
    PipelineStage.INDEXING: (ArtifactKind.INDEX_STATISTICS,),
}


# =============================================================================
# Canonical Document Model
# =============================================================================
class ContentBlockType(StrEnum):
    """Block types on a CDM page, in reading order."""

    PARAGRAPH = "paragraph"
    HEADING = "heading"
    TABLE = "table"
    LIST = "list"
    IMAGE = "image"
    HEADER = "header"
    FOOTER = "footer"
    FOOTNOTE = "footnote"
    SIGNATURE = "signature"
    CAPTION = "caption"
    PAGE_NUMBER = "page_number"
    FORMULA = "formula"


class ChunkType(StrEnum):
    """Semantic chunk types (§12). Fixed-size chunking is not among them."""

    SECTION = "section"
    CLAUSE = "clause"
    PARAGRAPH = "paragraph"
    TABLE = "table"
    LIST = "list"
    DEFINITION = "definition"
    APPENDIX = "appendix"
    SIGNATURE = "signature"
    FOOTNOTE = "footnote"


class ChunkStrategy(StrEnum):
    """Profile-selected chunking strategy. ``HYBRID`` is the default."""

    SECTION_BASED = "section_based"
    HEADING_AWARE = "heading_aware"
    CLAUSE_BASED = "clause_based"
    TABLE_PRESERVING = "table_preserving"
    LIST_PRESERVING = "list_preserving"
    HYBRID = "hybrid"


# =============================================================================
# Extracted knowledge
# =============================================================================
class AgreementType(StrEnum):
    """Contract taxonomy used by classification, DIP selection and filters."""

    MSA = "msa"
    NDA = "nda"
    VENDOR_AGREEMENT = "vendor_agreement"
    EMPLOYMENT_AGREEMENT = "employment_agreement"
    LEASE = "lease"
    CONSULTING_AGREEMENT = "consulting_agreement"
    GOVERNMENT_CONTRACT = "government_contract"
    HEALTHCARE_AGREEMENT = "healthcare_agreement"
    INSURANCE_POLICY = "insurance_policy"
    RESEARCH_COLLABORATION = "research_collaboration"
    SOW = "sow"
    PURCHASE_ORDER = "purchase_order"
    LICENSE_AGREEMENT = "license_agreement"
    SERVICE_AGREEMENT = "service_agreement"
    PARTNERSHIP_AGREEMENT = "partnership_agreement"
    AMENDMENT = "amendment"
    OTHER = "other"


class ClauseType(StrEnum):
    """Clause taxonomy. Extensible through the Clause Master without code change.

    ``clauses.clause_type`` is stored as text so admins can add categories at
    runtime; this enum is the seeded baseline and the typed constant set used by
    prompts, mandatory-clause checks and the UI.
    """

    TERM = "term"
    TERMINATION = "termination"
    TERMINATION_FOR_CONVENIENCE = "termination_for_convenience"
    TERMINATION_FOR_CAUSE = "termination_for_cause"
    LIQUIDATED_DAMAGES = "liquidated_damages"
    PAYMENT_TERMS = "payment_terms"
    PRICING = "pricing"
    CONFIDENTIALITY = "confidentiality"
    LICENSE_GRANT = "license_grant"
    EXCLUSIVITY = "exclusivity"
    AMENDMENT_PROCEDURE = "amendment_procedure"
    INTELLECTUAL_PROPERTY = "intellectual_property"
    INDEMNIFICATION = "indemnification"
    LIMITATION_OF_LIABILITY = "limitation_of_liability"
    WARRANTY = "warranty"
    GOVERNING_LAW = "governing_law"
    DISPUTE_RESOLUTION = "dispute_resolution"
    ARBITRATION = "arbitration"
    FORCE_MAJEURE = "force_majeure"
    ASSIGNMENT = "assignment"
    NON_COMPETE = "non_compete"
    NON_SOLICITATION = "non_solicitation"
    DATA_PROTECTION = "data_protection"
    SECURITY = "security"
    COMPLIANCE = "compliance"
    AUDIT_RIGHTS = "audit_rights"
    INSURANCE = "insurance"
    SERVICE_LEVEL = "service_level"
    RENEWAL = "renewal"
    AUTO_RENEWAL = "auto_renewal"
    NOTICE = "notice"
    AMENDMENT = "amendment"
    ENTIRE_AGREEMENT = "entire_agreement"
    SEVERABILITY = "severability"
    SUBCONTRACTING = "subcontracting"
    CHANGE_OF_CONTROL = "change_of_control"
    DEFINITIONS = "definitions"
    SCOPE_OF_WORK = "scope_of_work"
    ACCEPTANCE = "acceptance"
    DELIVERY = "delivery"
    TAXES = "taxes"
    EXPENSES = "expenses"
    PUBLICITY = "publicity"
    SURVIVAL = "survival"
    WAIVER = "waiver"
    COUNTERPARTS = "counterparts"
    OTHER = "other"


class LiabilityCapBasis(StrEnum):
    """How a limitation-of-liability cap is expressed.

    Drives the cap dropdown on the dedicated Limitation of Liability tab. The
    multiples are first-class values rather than free text because "1x vs 2x fees
    paid vs uncapped" is the single most-filtered commercial term in the
    repository.
    """

    ONE_X_FEES_PAID = "1x_fees_paid"
    TWO_X_FEES_PAID = "2x_fees_paid"
    OTHER_MULTIPLE_OF_FEES = "other_multiple_of_fees"
    FIXED_AMOUNT = "fixed_amount"
    FEES_PAID_IN_PERIOD = "fees_paid_in_period"
    UNCAPPED = "uncapped"
    NOT_SPECIFIED = "not_specified"

    @property
    def is_uncapped(self) -> bool:
        return self is LiabilityCapBasis.UNCAPPED


class LiabilityCarveOut(StrEnum):
    """Exclusions that escape the liability cap - i.e. unlimited exposure.

    Tracked separately from the cap itself: a contract can show "2x fees paid"
    and still carry unlimited exposure through carve-outs, which is exactly the
    risk a cap-only view hides.
    """

    IP_INFRINGEMENT = "ip_infringement"
    CONFIDENTIALITY_BREACH = "confidentiality_breach"
    GROSS_NEGLIGENCE = "gross_negligence"
    WILFUL_MISCONDUCT = "wilful_misconduct"
    FRAUD = "fraud"
    DEATH_OR_PERSONAL_INJURY = "death_or_personal_injury"
    DATA_PROTECTION_BREACH = "data_protection_breach"
    INDEMNIFICATION_OBLIGATIONS = "indemnification_obligations"
    PAYMENT_OBLIGATIONS = "payment_obligations"
    BREACH_OF_LAW = "breach_of_law"
    OTHER = "other"


class IndemnityPosture(StrEnum):
    """Direction and symmetry of an indemnity."""

    MUTUAL = "mutual"
    ONE_SIDED_IN_OUR_FAVOUR = "one_sided_in_our_favour"
    ONE_SIDED_AGAINST_US = "one_sided_against_us"
    NOT_SPECIFIED = "not_specified"


class PartySide(StrEnum):
    """Which side of the agreement a term binds or benefits.

    ``OUR_ORGANISATION`` is resolved by matching extracted party names against
    the configured organisation aliases (``ORGANIZATION_LEGAL_NAMES``), so
    questions like "can we terminate for convenience?" are answerable without
    hardcoding a company name anywhere in the pipeline.
    """

    OUR_ORGANISATION = "our_organisation"
    COUNTERPARTY = "counterparty"
    BOTH = "both"
    NEITHER = "neither"
    UNKNOWN = "unknown"


class LicenceExclusivity(StrEnum):
    EXCLUSIVE = "exclusive"
    NON_EXCLUSIVE = "non_exclusive"
    SOLE = "sole"
    NOT_SPECIFIED = "not_specified"


class EntityType(StrEnum):
    PARTY = "party"
    ORGANIZATION = "organization"
    PERSON = "person"
    VENDOR = "vendor"
    CUSTOMER = "customer"
    AFFILIATE = "affiliate"
    GUARANTOR = "guarantor"
    SIGNATORY = "signatory"
    GOVERNMENT_BODY = "government_body"


class PartyRole(StrEnum):
    DISCLOSING_PARTY = "disclosing_party"
    RECEIVING_PARTY = "receiving_party"
    SERVICE_PROVIDER = "service_provider"
    CUSTOMER = "customer"
    VENDOR = "vendor"
    SUPPLIER = "supplier"
    LICENSOR = "licensor"
    LICENSEE = "licensee"
    LANDLORD = "landlord"
    TENANT = "tenant"
    EMPLOYER = "employer"
    EMPLOYEE = "employee"
    CONTRACTOR = "contractor"
    OTHER = "other"


class RiskSeverity(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"

    @property
    def weight(self) -> int:
        """Weighting used by the 0-100 risk score (§7.4)."""
        return {
            RiskSeverity.CRITICAL: 40,
            RiskSeverity.HIGH: 25,
            RiskSeverity.MEDIUM: 10,
            RiskSeverity.LOW: 3,
        }[self]


class RiskType(StrEnum):
    UNLIMITED_LIABILITY = "unlimited_liability"
    UNCAPPED_INDEMNITY = "uncapped_indemnity"
    BROAD_TERMINATION_RIGHTS = "broad_termination_rights"
    UNILATERAL_TERMINATION = "unilateral_termination"
    AUTO_RENEWAL = "auto_renewal"
    SHORT_PAYMENT_TERMS = "short_payment_terms"
    LATE_PAYMENT_PENALTY = "late_payment_penalty"
    MISSING_LIABILITY_CAP = "missing_liability_cap"
    MISSING_MANDATORY_CLAUSE = "missing_mandatory_clause"
    UNFAVOURABLE_GOVERNING_LAW = "unfavourable_governing_law"
    IP_ASSIGNMENT_RISK = "ip_assignment_risk"
    DATA_PROTECTION_GAP = "data_protection_gap"
    COMPLIANCE_GAP = "compliance_gap"
    EXCLUSIVITY = "exclusivity"
    NON_COMPETE_BREADTH = "non_compete_breadth"
    CHANGE_OF_CONTROL = "change_of_control"
    NO_AUDIT_RIGHTS = "no_audit_rights"
    WEAK_SLA = "weak_sla"
    CURRENCY_EXPOSURE = "currency_exposure"
    AMBIGUOUS_SCOPE = "ambiguous_scope"
    OTHER = "other"


class RiskBand(StrEnum):
    """Derived from ``risk_score``: 0-33 low, 34-66 medium, 67-100 high."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

    @classmethod
    def from_score(cls, score: int | float | None) -> RiskBand:
        if score is None:
            return cls.LOW
        if score >= 67:
            return cls.HIGH
        if score >= 34:
            return cls.MEDIUM
        return cls.LOW


class DateType(StrEnum):
    EFFECTIVE_DATE = "effective_date"
    EXECUTION_DATE = "execution_date"
    EXPIRATION_DATE = "expiration_date"
    RENEWAL_DATE = "renewal_date"
    NOTICE_DEADLINE = "notice_deadline"
    MILESTONE = "milestone"
    PAYMENT_DUE = "payment_due"
    DELIVERY_DATE = "delivery_date"
    REVIEW_DATE = "review_date"
    TERMINATION_DATE = "termination_date"
    COMMENCEMENT_DATE = "commencement_date"
    OTHER = "other"


class ObligationStatus(StrEnum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    FULFILLED = "fulfilled"
    BREACHED = "breached"
    WAIVED = "waived"
    UNKNOWN = "unknown"


class ReviewStatus(StrEnum):
    """Human-review state of an AI-extracted item (§13 review triggers)."""

    NOT_REQUIRED = "not_required"
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CORRECTED = "corrected"


# =============================================================================
# Embeddings, graph, retrieval
# =============================================================================
class EmbeddingLevel(StrEnum):
    """Mandatory three-level embedding hierarchy (§14)."""

    DOCUMENT_SUMMARY = "document_summary"  # L1 - candidate selection
    CLAUSE = "clause"  # L2 - clause search / similarity / risk
    CHUNK = "chunk"  # L3 - RAG evidence retrieval


class GraphNodeType(StrEnum):
    CONTRACT = "contract"
    PARTY = "party"
    VENDOR = "vendor"
    CUSTOMER = "customer"
    CLAUSE = "clause"
    OBLIGATION = "obligation"
    RISK = "risk"
    DEFINITION = "definition"
    SCHEDULE = "schedule"
    AMENDMENT = "amendment"
    RENEWAL = "renewal"


class GraphRelation(StrEnum):
    REFERENCES = "references"
    DEPENDS_ON = "depends_on"
    DEFINES = "defines"
    AMENDS = "amends"
    REPLACES = "replaces"
    BELONGS_TO = "belongs_to"
    CONTAINS = "contains"
    GOVERNED_BY = "governed_by"
    ASSIGNED_TO = "assigned_to"
    RENEWED_BY = "renewed_by"


class SearchScope(StrEnum):
    APPLICATION = "application"
    PROJECT = "project"
    CONTRACT = "contract"


class SearchMode(StrEnum):
    KEYWORD = "keyword"
    SEMANTIC = "semantic"
    HYBRID = "hybrid"


class QueryIntent(StrEnum):
    """Detected by the Retrieval Planner; drives strategy selection (§15)."""

    METADATA_LOOKUP = "metadata_lookup"
    CLAUSE_LOOKUP = "clause_lookup"
    OBLIGATION_LOOKUP = "obligation_lookup"
    RISK_ASSESSMENT = "risk_assessment"
    COMPARISON = "comparison"
    SUMMARIZATION = "summarization"
    TIMELINE = "timeline"
    RELATIONSHIP = "relationship"
    COMPLIANCE = "compliance"
    FINANCIAL = "financial"
    GENERAL_QA = "general_qa"


class RetrievalStrategy(StrEnum):
    """The six strategies from §15. ``HYBRID`` is the recommended default."""

    METADATA_ONLY = "metadata_only"
    METADATA_PLUS_DOCUMENT = "metadata_plus_document"
    CLAUSE_RETRIEVAL = "clause_retrieval"
    CHUNK_RETRIEVAL = "chunk_retrieval"
    GRAPH_TRAVERSAL = "graph_traversal"
    HYBRID = "hybrid"


class ResponseFormat(StrEnum):
    """Output formats the Prompt Orchestrator can request (§16)."""

    NATURAL_LANGUAGE = "natural_language"
    JSON = "json"
    EXECUTIVE_SUMMARY = "executive_summary"
    RISK_REPORT = "risk_report"
    COMPLIANCE_REPORT = "compliance_report"
    CLAUSE_COMPARISON = "clause_comparison"
    TIMELINE = "timeline"
    ACTION_ITEMS = "action_items"
    CONTRACT_SUMMARY = "contract_summary"
    OBLIGATION_REPORT = "obligation_report"


class ConfidenceBand(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"

    @classmethod
    def from_score(cls, score: float) -> ConfidenceBand:
        if score >= 0.8:
            return cls.HIGH
        if score >= 0.55:
            return cls.MEDIUM
        return cls.LOW


class ChatRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


# =============================================================================
# Alerts, export, audit
# =============================================================================
class AlertType(StrEnum):
    CONTRACT_EXPIRING = "contract_expiring"
    HIGH_RISK = "high_risk"
    MISSING_MANDATORY_CLAUSE = "missing_mandatory_clause"
    PROCESSING_FAILED = "processing_failed"
    AUTO_RENEWAL_NOTICE = "auto_renewal_notice"
    OBLIGATION_DUE = "obligation_due"
    REVIEW_REQUIRED = "review_required"


class AlertSeverity(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class AlertStatus(StrEnum):
    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"


class ExportFormat(StrEnum):
    XLSX = "xlsx"
    CSV = "csv"
    JSON = "json"
    PDF = "pdf"


class ExportStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    EXPIRED = "expired"


class AuditAction(StrEnum):
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    LOGIN = "login"
    LOGIN_FAILED = "login_failed"
    LOGOUT = "logout"
    UPLOAD = "upload"
    DOWNLOAD = "download"
    EXPORT = "export"
    SEARCH = "search"
    COPILOT_QUERY = "copilot_query"
    JOB_RETRY = "job_retry"
    JOB_CANCEL = "job_cancel"
    JOB_PAUSE = "job_pause"
    JOB_RESUME = "job_resume"
    REVIEW_DECISION = "review_decision"
    PERMISSION_CHANGE = "permission_change"
    CONFIG_CHANGE = "config_change"


__all__ = [
    "STAGE_ARTIFACTS",
    "STAGE_DEPENDENCIES",
    "STAGE_ORDER",
    "STAGE_TO_STATE",
    "AgreementType",
    "AlertSeverity",
    "AlertStatus",
    "AlertType",
    "ArtifactKind",
    "AuditAction",
    "AuthProvider",
    "ChatRole",
    "ChunkStrategy",
    "ChunkType",
    "ClauseType",
    "ConfidenceBand",
    "ContentBlockType",
    "ContractStatus",
    "DateType",
    "EmbeddingLevel",
    "EntityType",
    "ExportFormat",
    "ExportStatus",
    "FileType",
    "GraphNodeType",
    "GraphRelation",
    "IndemnityPosture",
    "JobPriority",
    "JobState",
    "LiabilityCapBasis",
    "LiabilityCarveOut",
    "LicenceExclusivity",
    "ObligationStatus",
    "PartyRole",
    "PartySide",
    "Permission",
    "PipelineStage",
    "ProjectStatus",
    "QueryIntent",
    "ResponseFormat",
    "RetrievalStrategy",
    "ReviewStatus",
    "RiskBand",
    "RiskSeverity",
    "RiskType",
    "RoleName",
    "SearchMode",
    "SearchScope",
    "StageStatus",
    "stage_index",
    "stages_from",
]
