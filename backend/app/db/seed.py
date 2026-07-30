"""Idempotent seed data.

Run by ``python -m app.cli seed`` (and by ``migrate --seed`` in the compose
stack). Every function is safe to re-run: it upserts by natural key rather than
inserting blindly, so seeding a live database adds what is missing and leaves
operator edits alone.

Seeds:
  * the four platform roles and their permission sets,
  * the default System Administrator,
  * the Clause Master taxonomy from :mod:`app.db.clause_seeds` (priority-ordered),
  * the ten Document Intelligence Profiles from §11,
  * platform-default alert rules and AI settings.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.deps import ADMIN_EXCLUDED_PERMISSIONS
from app.core.enums import (
    AlertSeverity,
    AlertType,
    ChunkStrategy,
    ClauseType,
    Permission,
    RoleName,
)
from app.core.logging import get_logger
from app.core.security import hash_password
from app.core.versions import PROFILE_SEED_VERSION
from app.db.clause_seeds import ALL_CLAUSE_SEEDS, PRIORITY_CLAUSE_ORDER
from app.models import (
    AISettings,
    AlertRule,
    ClauseMasterCategory,
    ClauseMasterRule,
    DocumentProfile,
    Role,
    User,
)

logger = get_logger(__name__)


# =============================================================================
# Roles
# =============================================================================
#: Everything except the capabilities administration is deliberately kept out of.
#: Held by System Admin; the ``is_system_admin`` flag is what actually grants the
#: bypass, so this list is documentation plus a safety net.
#:
#: The subtraction matters even though an administrator's permissions are resolved
#: from the flag rather than from this row: the role is assignable to an ordinary
#: member through project membership, and a role row that still listed
#: ``contract:upload`` would hand back the capability that way.
_ALL_PERMISSIONS = sorted({p.value for p in Permission} - ADMIN_EXCLUDED_PERMISSIONS)

_PROJECT_MANAGER_PERMISSIONS = [
    Permission.PROJECT_READ,
    Permission.PROJECT_UPDATE,
    Permission.PROJECT_MEMBER_MANAGE,
    Permission.CONTRACT_UPLOAD,
    Permission.CONTRACT_READ,
    Permission.CONTRACT_UPDATE,
    Permission.CONTRACT_DELETE,
    Permission.CONTRACT_DOWNLOAD,
    Permission.JOB_READ,
    Permission.JOB_CONTROL,
    Permission.KNOWLEDGE_READ,
    Permission.KNOWLEDGE_REVIEW,
    Permission.SEARCH_EXECUTE,
    Permission.COPILOT_USE,
    Permission.REPORT_VIEW,
    Permission.EXPORT_CREATE,
    Permission.AUDIT_READ,
]

_REVIEWER_PERMISSIONS = [
    Permission.PROJECT_READ,
    Permission.CONTRACT_UPLOAD,
    Permission.CONTRACT_READ,
    Permission.CONTRACT_DOWNLOAD,
    Permission.JOB_READ,
    Permission.KNOWLEDGE_READ,
    Permission.KNOWLEDGE_REVIEW,
    Permission.SEARCH_EXECUTE,
    Permission.COPILOT_USE,
    Permission.REPORT_VIEW,
    Permission.EXPORT_CREATE,
]

_VIEWER_PERMISSIONS = [
    Permission.PROJECT_READ,
    Permission.CONTRACT_READ,
    Permission.CONTRACT_DOWNLOAD,
    Permission.JOB_READ,
    Permission.KNOWLEDGE_READ,
    Permission.SEARCH_EXECUTE,
    Permission.COPILOT_USE,
    Permission.REPORT_VIEW,
]

ROLE_SEEDS: tuple[dict[str, Any], ...] = (
    {
        "name": RoleName.SYSTEM_ADMIN,
        # UI label: "Admin" - system & master-data management.
        "display_name": "Admin",
        "description": (
            "Full platform access: system configuration, users, master data, all projects."
        ),
        "permissions": _ALL_PERMISSIONS,
        "rank": 400,
    },
    {
        "name": RoleName.PROJECT_MANAGER,
        # UI label: "Contract Manager" - manage contracts & insights.
        "display_name": "Contract Manager",
        "description": "Manage contracts, processing and insights within assigned projects.",
        "permissions": [p.value for p in _PROJECT_MANAGER_PERMISSIONS],
        "rank": 300,
    },
    {
        "name": RoleName.REVIEWER,
        "display_name": "Reviewer",
        "description": "Upload contracts and review or correct AI-extracted knowledge.",
        "permissions": [p.value for p in _REVIEWER_PERMISSIONS],
        "rank": 200,
    },
    {
        "name": RoleName.VIEWER,
        # UI label: "Viewer" - view contracts & reports.
        "display_name": "Viewer",
        "description": "Read-only access to contracts, search, insights and reports.",
        "permissions": [p.value for p in _VIEWER_PERMISSIONS],
        "rank": 100,
    },
)


async def seed_roles(db: AsyncSession) -> dict[RoleName, Role]:
    """Create or refresh the four platform roles.

    Permissions are re-applied on every run so a code change to a role's
    permission set reaches existing deployments without a manual step.
    """
    result: dict[RoleName, Role] = {}
    for spec in ROLE_SEEDS:
        existing = (
            await db.execute(select(Role).where(Role.name == spec["name"]))
        ).scalar_one_or_none()
        if existing is None:
            role = Role(
                name=spec["name"],
                display_name=spec["display_name"],
                description=spec["description"],
                permissions=spec["permissions"],
                rank=spec["rank"],
                is_system=True,
            )
            db.add(role)
            logger.info("seed_role_created", role=str(spec["name"]))
        else:
            existing.display_name = spec["display_name"]
            existing.description = spec["description"]
            existing.permissions = spec["permissions"]
            existing.rank = spec["rank"]
            role = existing
        result[spec["name"]] = role

    await db.flush()
    return result


# =============================================================================
# Default administrator
# =============================================================================
async def seed_admin_user(db: AsyncSession) -> User:
    """Create the seeded System Administrator if absent.

    An existing account is never modified - re-seeding must not reset a password
    the operator has already changed.
    """
    settings = get_settings()
    email = settings.security.seed_admin_email

    existing = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if existing is not None:
        if not existing.is_system_admin:
            existing.is_system_admin = True
            logger.warning("seed_admin_flag_restored", email=email)
        return existing

    user = User(
        email=email,
        full_name=settings.security.seed_admin_name,
        password_hash=hash_password(settings.security.seed_admin_password),
        is_active=True,
        is_system_admin=True,
        must_change_password=settings.security.seed_admin_force_password_change,
    )
    db.add(user)
    await db.flush()
    logger.info("seed_admin_created", email=email)
    return user


# =============================================================================
# Clause Master
# =============================================================================
async def seed_clause_master(db: AsyncSession, created_by: Any = None) -> int:
    """Seed the priority-ordered clause taxonomy and one active rule per category.

    Definitions live in :mod:`app.db.clause_seeds`. Existing categories keep their
    operator-tuned thresholds, mandatory flags and UI placement - re-seeding must
    not undo an administrator's configuration - so only genuinely new categories
    are inserted.
    """
    created = 0
    for seed in ALL_CLAUSE_SEEDS:
        key_value = str(seed.key)
        existing = (
            await db.execute(
                select(ClauseMasterCategory).where(ClauseMasterCategory.key == key_value)
            )
        ).scalar_one_or_none()
        if existing is not None:
            continue

        category = ClauseMasterCategory(
            key=key_value,
            name=seed.name,
            description=(
                seed.notes
                or f"{seed.name} clause - extracted, validated and indexed for retrieval."
            ),
            group_name=seed.group_name,
            mandatory=seed.mandatory,
            confidence_threshold=seed.confidence_threshold,
            default_risk_severity=seed.missing_severity,
            is_active=True,
            is_system=True,
            priority=seed.priority,
            display_order=seed.priority * 10,
            ui_config=seed.ui_config,
            created_by=created_by,
        )
        db.add(category)
        await db.flush()

        db.add(
            ClauseMasterRule(
                category_id=category.id,
                version=1,
                extraction_rule=seed.extraction_rule,
                prompt_template_id="extraction.clauses",
                synonyms=seed.synonyms,
                output_schema=seed.output_schema,
                validation_rules={},
                is_active=True,
                created_by=created_by,
                change_note="Seeded baseline rule.",
            )
        )
        created += 1

    await db.flush()
    if created:
        logger.info("seed_clause_master", categories_created=created, total=len(ALL_CLAUSE_SEEDS))
    return created


# =============================================================================
# Document Intelligence Profiles
# =============================================================================
_COMMON_REVIEW_RULES = {
    "low_confidence": True,
    "missing_mandatory_clause": True,
    "conflicting_dates": True,
    "high_risk_clause": True,
    "validation_failure": True,
}

_COMMON_VALIDATION_RULES = {
    "dates": {
        "effective_before_expiration": True,
        "execution_not_after_effective": False,
        "reject_impossible_dates": True,
    },
    "money": {"require_currency_with_amount": True, "max_reasonable_value": 1_000_000_000},
    "references": {"resolve_cross_references": True, "flag_unresolved": True},
}

_ALL_LEVELS = ["document_summary", "clause", "chunk"]

#: The seven highest-priority clauses. Every commercial contract type is expected
#: to contain these, so they form the mandatory baseline that profiles extend.
_TOP_PRIORITY_MANDATORY = [
    ClauseType.LIMITATION_OF_LIABILITY,
    ClauseType.INDEMNIFICATION,
    ClauseType.INTELLECTUAL_PROPERTY,
    ClauseType.PAYMENT_TERMS,
    ClauseType.TERM,
    ClauseType.GOVERNING_LAW,
    ClauseType.CONFIDENTIALITY,
]

#: Commonly present but not universally required.
_COMMON_OPTIONAL = [
    ClauseType.AUTO_RENEWAL,
    ClauseType.DISPUTE_RESOLUTION,
    ClauseType.SURVIVAL,
    ClauseType.TERMINATION_FOR_CONVENIENCE,
    ClauseType.TERMINATION_FOR_CAUSE,
    ClauseType.LIQUIDATED_DAMAGES,
    ClauseType.INSURANCE,
    ClauseType.WARRANTY,
    ClauseType.FORCE_MAJEURE,
    ClauseType.LICENSE_GRANT,
    ClauseType.NON_SOLICITATION,
    ClauseType.EXCLUSIVITY,
    ClauseType.ASSIGNMENT,
    ClauseType.AUDIT_RIGHTS,
    ClauseType.NOTICE,
    ClauseType.AMENDMENT_PROCEDURE,
]


def _keys(clauses: list[ClauseType]) -> list[str]:
    return [str(clause) for clause in clauses]


def _optional_excluding(mandatory: list[ClauseType], *extra: ClauseType) -> list[str]:
    """Every priority clause not already mandatory for this profile.

    Keeps a profile's optional list exhaustive without repeating 23 keys per
    profile: anything in the priority taxonomy that is not mandatory here is, by
    definition, worth extracting if present.
    """
    mandatory_keys = {str(clause) for clause in mandatory}
    optional = [key for key in PRIORITY_CLAUSE_ORDER if key not in mandatory_keys]
    for clause in extra:
        key = str(clause)
        if key not in mandatory_keys and key not in optional:
            optional.append(key)
    return optional


def _profile(
    *,
    key: str,
    name: str,
    category: str,
    contract_type: str,
    mandatory: list[ClauseType],
    risk_weights: dict[str, str],
    optional: list[str] | None = None,
    compliance: list[dict[str, Any]] | None = None,
    chunk_strategy: ChunkStrategy = ChunkStrategy.HYBRID,
    chunk_config: dict[str, Any] | None = None,
    workflow_extensions: list[str] | None = None,
    retain_years: int = 7,
    threshold: float = 0.85,
    is_default: bool = False,
    classification_hints: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "key": key,
        "version": PROFILE_SEED_VERSION,
        "name": name,
        "category": category,
        "contract_type": contract_type,
        "description": f"Processing profile for {name}.",
        "supported_languages": ["en"],
        "is_active": True,
        "is_default": is_default,
        "priority": 100,
        "classification_hints": classification_hints or {},
        "extraction_strategy": {
            "categories": [
                "metadata",
                "parties",
                "clauses",
                "financial",
                "obligations",
                "rights",
                "risks",
                "dates",
                "relationships",
            ],
            "prompt_templates": {
                "metadata": "extraction.metadata",
                "parties": "extraction.parties",
                "clauses": "extraction.clauses",
                "financial": "extraction.financial",
                "obligations": "extraction.obligations",
                "rights": "extraction.rights",
                "risks": "extraction.risks",
                "dates": "extraction.dates",
                "relationships": "extraction.relationships",
            },
            # Highest-priority clauses are extracted first, so a job that fails
            # part-way through still produced the terms that matter most.
            "clause_priority_order": list(PRIORITY_CLAUSE_ORDER),
            "validation_hints": [
                "Quote clause text verbatim; never paraphrase into the text field.",
                "Return null rather than guessing an absent value.",
                "Resolve party-side attributes against the configured organisation aliases.",
            ],
        },
        "mandatory_clauses": _keys(mandatory),
        "optional_clauses": optional if optional is not None else _optional_excluding(mandatory),
        "confidence_threshold": threshold,
        "review_rules": _COMMON_REVIEW_RULES,
        "chunk_strategy": chunk_strategy,
        "chunk_config": chunk_config
        or {
            "max_tokens": 900,
            "min_tokens": 40,
            "overlap_tokens": 80,
            "preserve_tables": True,
            "preserve_lists": True,
            "merge_cross_page_clauses": True,
        },
        "embedding_config": {
            "levels": _ALL_LEVELS,
            "similarity_threshold": 0.25,
            "summary_includes": ["summary", "key_topics", "parties", "agreement_type"],
            "metadata_weighting": {"agreement_type": 0.2, "vendor": 0.1, "category": 0.1},
        },
        "risk_mapping": {
            "weights": risk_weights,
            "score": {"low_max": 33, "medium_max": 66},
        },
        "compliance_rules": compliance or [],
        "validation_rules": _COMMON_VALIDATION_RULES,
        "workflow_extensions": workflow_extensions or [],
        "retention_policy": {
            "retain_years": retain_years,
            "purge_artifacts_after_days": None,
            "legal_hold": False,
        },
    }


#: Baseline risk weighting. Keyed by RiskType value -> RiskSeverity value.
_STANDARD_RISK_WEIGHTS = {
    "unlimited_liability": "critical",
    "uncapped_indemnity": "critical",
    "missing_liability_cap": "high",
    "broad_termination_rights": "high",
    "unilateral_termination": "high",
    "auto_renewal": "medium",
    "short_payment_terms": "medium",
    "missing_mandatory_clause": "high",
    "ip_assignment_risk": "high",
    "no_audit_rights": "low",
    "ambiguous_scope": "medium",
    "exclusivity": "medium",
    "change_of_control": "medium",
    "non_compete_breadth": "medium",
    "weak_sla": "low",
    "currency_exposure": "low",
}


PROFILE_SEEDS: tuple[dict[str, Any], ...] = (
    _profile(
        key="commercial_msa",
        name="Commercial Master Services Agreement",
        category="Commercial",
        contract_type="msa",
        mandatory=[
            *_TOP_PRIORITY_MANDATORY,
            ClauseType.TERMINATION_FOR_CAUSE,
            ClauseType.SURVIVAL,
        ],
        risk_weights=_STANDARD_RISK_WEIGHTS,
        compliance=[{"pack": "procurement", "level": "standard"}],
        classification_hints={
            "title_patterns": ["master services agreement", "msa", "master agreement"],
            "required_phrases": ["services", "statement of work"],
            "min_score": 0.4,
        },
        is_default=True,
    ),
    _profile(
        key="vendor_agreement",
        name="Vendor Agreement",
        category="Procurement",
        contract_type="vendor_agreement",
        mandatory=[
            *_TOP_PRIORITY_MANDATORY,
            ClauseType.TERMINATION_FOR_CAUSE,
            ClauseType.AUDIT_RIGHTS,
            ClauseType.INSURANCE,
        ],
        risk_weights={**_STANDARD_RISK_WEIGHTS, "no_audit_rights": "medium"},
        compliance=[
            {"pack": "procurement", "level": "strict"},
            {"pack": "anti_bribery", "level": "standard"},
        ],
        classification_hints={
            "title_patterns": ["vendor agreement", "supplier agreement", "purchase agreement"],
            "required_phrases": ["vendor", "supplier"],
            "min_score": 0.4,
        },
    ),
    _profile(
        key="nda",
        name="Non-Disclosure Agreement",
        category="Legal",
        contract_type="nda",
        mandatory=[
            ClauseType.CONFIDENTIALITY,
            ClauseType.TERM,
            ClauseType.GOVERNING_LAW,
            ClauseType.SURVIVAL,
        ],
        risk_weights={
            "missing_mandatory_clause": "high",
            "unfavourable_governing_law": "medium",
            "non_compete_breadth": "medium",
            "ip_assignment_risk": "medium",
            "auto_renewal": "low",
        },
        # NDAs are short and clause-dense; clause-based chunking beats hybrid here.
        chunk_strategy=ChunkStrategy.CLAUSE_BASED,
        chunk_config={
            "max_tokens": 600,
            "min_tokens": 30,
            "overlap_tokens": 40,
            "preserve_tables": True,
            "preserve_lists": True,
            "merge_cross_page_clauses": True,
        },
        retain_years=5,
        classification_hints={
            "title_patterns": ["non-disclosure", "nda", "confidentiality agreement"],
            "required_phrases": ["confidential information"],
            "min_score": 0.5,
        },
    ),
    _profile(
        key="employment_agreement",
        name="Employment Agreement",
        category="HR",
        contract_type="employment_agreement",
        mandatory=[
            ClauseType.TERM,
            ClauseType.TERMINATION_FOR_CAUSE,
            ClauseType.TERMINATION_FOR_CONVENIENCE,
            ClauseType.CONFIDENTIALITY,
            ClauseType.INTELLECTUAL_PROPERTY,
            ClauseType.GOVERNING_LAW,
            ClauseType.PAYMENT_TERMS,
        ],
        risk_weights={
            "non_compete_breadth": "high",
            "ip_assignment_risk": "high",
            "missing_mandatory_clause": "high",
            "broad_termination_rights": "medium",
            "data_protection_gap": "medium",
        },
        compliance=[{"pack": "employment_law", "level": "standard"}],
        # Employment agreements routinely require review: they carry personal data
        # and restrictive covenants.
        workflow_extensions=["human_review"],
        retain_years=10,
        classification_hints={
            "title_patterns": ["employment agreement", "offer letter", "contract of employment"],
            "required_phrases": ["employee", "employment"],
            "min_score": 0.45,
        },
    ),
    _profile(
        key="lease",
        name="Lease Agreement",
        category="Real Estate",
        contract_type="lease",
        mandatory=[
            ClauseType.TERM,
            ClauseType.PAYMENT_TERMS,
            ClauseType.TERMINATION_FOR_CAUSE,
            ClauseType.GOVERNING_LAW,
            ClauseType.INSURANCE,
            ClauseType.AUTO_RENEWAL,
        ],
        risk_weights={
            "auto_renewal": "high",
            "missing_mandatory_clause": "high",
            "liquidated_damages": "medium",
            "short_payment_terms": "low",
            "currency_exposure": "medium",
        },
        # Leases are schedule- and table-heavy (rent tables, premises schedules).
        chunk_strategy=ChunkStrategy.TABLE_PRESERVING,
        retain_years=12,
        classification_hints={
            "title_patterns": ["lease", "tenancy agreement", "rental agreement"],
            "required_phrases": ["premises", "landlord", "tenant"],
            "min_score": 0.45,
        },
    ),
    _profile(
        key="consulting_agreement",
        name="Consulting Agreement",
        category="Professional Services",
        contract_type="consulting_agreement",
        mandatory=[
            ClauseType.SCOPE_OF_WORK,
            ClauseType.PAYMENT_TERMS,
            ClauseType.TERM,
            ClauseType.TERMINATION_FOR_CONVENIENCE,
            ClauseType.CONFIDENTIALITY,
            ClauseType.INTELLECTUAL_PROPERTY,
            ClauseType.LIMITATION_OF_LIABILITY,
        ],
        risk_weights={**_STANDARD_RISK_WEIGHTS, "ambiguous_scope": "high"},
        classification_hints={
            "title_patterns": ["consulting agreement", "consultancy", "professional services"],
            "required_phrases": ["consultant", "services"],
            "min_score": 0.4,
        },
    ),
    _profile(
        key="government_contract",
        name="Government Contract",
        category="Public Sector",
        contract_type="government_contract",
        mandatory=[
            ClauseType.SCOPE_OF_WORK,
            ClauseType.PAYMENT_TERMS,
            ClauseType.TERM,
            ClauseType.TERMINATION_FOR_CONVENIENCE,
            ClauseType.TERMINATION_FOR_CAUSE,
            ClauseType.COMPLIANCE,
            ClauseType.AUDIT_RIGHTS,
            ClauseType.GOVERNING_LAW,
            ClauseType.LIQUIDATED_DAMAGES,
        ],
        risk_weights={
            **_STANDARD_RISK_WEIGHTS,
            "compliance_gap": "critical",
            "no_audit_rights": "high",
        },
        compliance=[
            {"pack": "procurement", "level": "strict"},
            {"pack": "sox", "level": "standard"},
            {"pack": "anti_bribery", "level": "strict"},
        ],
        # Public-sector work carries statutory obligations; both a compliance pass
        # and legal sign-off are warranted before the contract is treated as READY.
        workflow_extensions=["compliance_review", "legal_approval"],
        retain_years=10,
        threshold=0.90,
        classification_hints={
            "title_patterns": ["government", "public sector", "federal", "municipal"],
            "required_phrases": ["contracting officer", "agency", "public body"],
            "min_score": 0.45,
        },
    ),
    _profile(
        key="healthcare_agreement",
        name="Healthcare Agreement",
        category="Healthcare",
        contract_type="healthcare_agreement",
        mandatory=[
            *_TOP_PRIORITY_MANDATORY,
            ClauseType.DATA_PROTECTION,
            ClauseType.COMPLIANCE,
            ClauseType.AUDIT_RIGHTS,
        ],
        risk_weights={
            **_STANDARD_RISK_WEIGHTS,
            "data_protection_gap": "critical",
            "compliance_gap": "critical",
        },
        compliance=[
            {"pack": "hipaa", "level": "strict"},
            {"pack": "gdpr", "level": "standard"},
        ],
        workflow_extensions=["compliance_review", "human_review"],
        retain_years=10,
        threshold=0.90,
        classification_hints={
            "title_patterns": ["business associate", "healthcare", "clinical", "patient"],
            "required_phrases": ["protected health information", "patient", "clinical"],
            "min_score": 0.45,
        },
    ),
    _profile(
        key="insurance_policy",
        name="Insurance Policy",
        category="Insurance",
        contract_type="insurance_policy",
        mandatory=[
            ClauseType.TERM,
            ClauseType.PAYMENT_TERMS,
            ClauseType.LIMITATION_OF_LIABILITY,
            ClauseType.GOVERNING_LAW,
            ClauseType.INSURANCE,
            ClauseType.NOTICE,
        ],
        risk_weights={
            "missing_liability_cap": "high",
            "auto_renewal": "medium",
            "currency_exposure": "medium",
            "missing_mandatory_clause": "high",
        },
        # Policies are schedule-driven: coverage tables must survive chunking intact.
        chunk_strategy=ChunkStrategy.TABLE_PRESERVING,
        retain_years=10,
        classification_hints={
            "title_patterns": ["policy", "insurance", "certificate of insurance"],
            "required_phrases": ["insured", "coverage", "premium"],
            "min_score": 0.45,
        },
    ),
    _profile(
        key="research_collaboration",
        name="Research Collaboration Agreement",
        category="Research",
        contract_type="research_collaboration",
        mandatory=[
            ClauseType.SCOPE_OF_WORK,
            ClauseType.INTELLECTUAL_PROPERTY,
            ClauseType.LICENSE_GRANT,
            ClauseType.CONFIDENTIALITY,
            ClauseType.TERM,
            ClauseType.PUBLICITY,
            ClauseType.GOVERNING_LAW,
        ],
        risk_weights={
            "ip_assignment_risk": "critical",
            "missing_mandatory_clause": "high",
            "ambiguous_scope": "high",
            "exclusivity": "high",
        },
        workflow_extensions=["legal_approval"],
        retain_years=10,
        classification_hints={
            "title_patterns": ["research", "collaboration agreement", "joint development"],
            "required_phrases": ["research", "collaboration", "publication"],
            "min_score": 0.45,
        },
    ),
)


async def seed_document_profiles(db: AsyncSession, created_by: Any = None) -> int:
    """Seed the ten baseline Document Intelligence Profiles.

    Keyed by ``(key, version)``. An existing profile version is left untouched -
    contracts already reference it and must keep the exact configuration that
    processed them (§11).
    """
    created = 0
    for spec in PROFILE_SEEDS:
        existing = (
            await db.execute(
                select(DocumentProfile).where(
                    DocumentProfile.key == spec["key"],
                    DocumentProfile.version == spec["version"],
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            continue

        db.add(DocumentProfile(**spec, created_by=created_by))
        created += 1

    await db.flush()
    if created:
        logger.info("seed_document_profiles", profiles_created=created)
    return created


# =============================================================================
# Alert rules & AI settings
# =============================================================================
async def seed_alert_rules(db: AsyncSession) -> int:
    """Seed the platform-default alert rules (NULL project = global default)."""
    settings = get_settings()
    defaults: tuple[tuple[AlertType, AlertSeverity, dict[str, Any], int | None], ...] = (
        (
            AlertType.CONTRACT_EXPIRING,
            AlertSeverity.MEDIUM,
            {
                "window_days": settings.alerts.expiry_window_days,
                "escalate_days": 30,
                "critical_days": 7,
            },
            30,
        ),
        (
            AlertType.HIGH_RISK,
            AlertSeverity.HIGH,
            {"risk_score_cutoff": settings.alerts.risk_score_cutoff},
            None,
        ),
        (
            AlertType.MISSING_MANDATORY_CLAUSE,
            AlertSeverity.HIGH,
            # Empty list means "whatever the contract's profile marks mandatory".
            {"clause_types": [], "min_missing": 1},
            None,
        ),
        (AlertType.PROCESSING_FAILED, AlertSeverity.CRITICAL, {"after_retries": 3}, None),
        (
            # Auto-renewal needs a longer lead time than plain expiry: the notice
            # deadline falls before the expiry date, not on it.
            AlertType.AUTO_RENEWAL_NOTICE,
            AlertSeverity.HIGH,
            {"lead_days": 45},
            15,
        ),
        (AlertType.OBLIGATION_DUE, AlertSeverity.MEDIUM, {"window_days": 14}, 7),
        (AlertType.REVIEW_REQUIRED, AlertSeverity.LOW, {"min_items": 1}, None),
    )

    created = 0
    for alert_type, severity, config, escalate in defaults:
        existing = (
            await db.execute(
                select(AlertRule).where(
                    AlertRule.alert_type == alert_type,
                    AlertRule.project_id.is_(None),
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            continue

        db.add(
            AlertRule(
                project_id=None,
                # Named from the type, so the administration screen reads as prose
                # rather than as enum values.
                name=alert_type.value.replace("_", " ").capitalize(),
                alert_type=alert_type,
                severity=severity,
                is_enabled=True,
                config=config,
                escalate_after_days=escalate,
                notify_channels=["in_app"],
            )
        )
        created += 1

    await db.flush()
    if created:
        logger.info("seed_alert_rules", rules_created=created)
    return created


async def seed_ai_settings(db: AsyncSession) -> bool:
    """Seed the single current AI settings row from deployed configuration."""
    existing = (
        await db.execute(select(AISettings).where(AISettings.is_current.is_(True)))
    ).scalar_one_or_none()
    if existing is not None:
        return False

    settings = get_settings()
    db.add(
        AISettings(
            is_current=True,
            providers={
                "llm": {
                    "provider": settings.llm.provider,
                    "model": settings.llm.model,
                    "model_simple": settings.llm.model_simple,
                    "model_complex": settings.llm.model_complex,
                    # Reasoning effort, not temperature: current Claude models reject
                    # temperature/top_p/top_k outright, so the setting does not exist.
                    # Effort is the equivalent quality dial and lives in output_config.
                    "effort": settings.llm.effort,
                    "max_output_tokens": settings.llm.max_output_tokens,
                },
                "embedding": {
                    "provider": settings.embedding.provider,
                    "model": settings.embedding.model,
                    "dim": settings.embedding.dim,
                },
                "reranker": {
                    "enabled": settings.retrieval.reranker_enabled,
                    "model": settings.retrieval.reranker_model,
                },
            },
            thresholds={
                "review_confidence": float(settings.review_confidence_threshold),
                "retrieval_min_similarity": float(settings.retrieval.min_similarity),
                "context_token_budget": settings.retrieval.context_token_budget,
                "rerank_top_k": settings.retrieval.rerank_top_k,
                "max_documents": settings.retrieval.max_documents,
                "max_clauses": settings.retrieval.max_clauses,
                "max_chunks": settings.retrieval.max_chunks,
            },
            model_routing={
                "simple": settings.llm.model_simple,
                "default": settings.llm.model,
                "complex": settings.llm.model_complex,
                # Intents routed to the stronger model - multi-document reasoning
                # is where a cheaper model starts inventing.
                "complex_intents": ["comparison", "risk_assessment", "compliance"],
            },
            policies={
                "answer_only_from_evidence": True,
                "require_citations": True,
                "declare_insufficient_evidence": True,
                "never_provide_legal_advice": True,
                "mask_confidential_clauses_for_viewers": True,
                "organisation_aliases": settings.organization_legal_names,
            },
            feature_flags={
                "streaming": True,
                "graph_retrieval": True,
                "human_review": True,
                "ocr": settings.parser.ocr_enabled,
                "cross_project_admin_search": True,
            },
            change_note="Seeded from deployment configuration.",
        )
    )
    await db.flush()
    logger.info("seed_ai_settings_created")
    return True


# =============================================================================
# Entry point
# =============================================================================
async def seed_all(db: AsyncSession) -> dict[str, Any]:
    """Run every seed in dependency order. Idempotent."""
    roles = await seed_roles(db)
    admin = await seed_admin_user(db)
    clauses = await seed_clause_master(db, created_by=admin.id)
    profiles = await seed_document_profiles(db, created_by=admin.id)
    alert_rules = await seed_alert_rules(db)
    ai_settings = await seed_ai_settings(db)

    summary = {
        "roles": len(roles),
        "admin_email": admin.email,
        "clause_categories_created": clauses,
        "clause_categories_total": len(ALL_CLAUSE_SEEDS),
        "profiles_created": profiles,
        "alert_rules_created": alert_rules,
        "ai_settings_created": ai_settings,
    }
    logger.info("seed_complete", **summary)
    return summary


__all__ = [
    "PROFILE_SEEDS",
    "ROLE_SEEDS",
    "seed_admin_user",
    "seed_ai_settings",
    "seed_alert_rules",
    "seed_all",
    "seed_clause_master",
    "seed_document_profiles",
    "seed_roles",
]
