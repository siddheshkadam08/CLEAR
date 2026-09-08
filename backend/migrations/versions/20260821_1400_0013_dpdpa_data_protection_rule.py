"""Teach the data-protection clause to recognise India's DPDP Act.

The seeded extraction rule for ``data_protection`` was GDPR-shaped: its headings
and keywords were "gdpr", "data controller", "data processor", "data subject".
A clause drafted to the Digital Personal Data Protection Act 2023 shares almost
none of that vocabulary - the Act says **Data Fiduciary** and **Data Principal** -
so a compliant Indian privacy clause scored as no clause at all, and the contract
came out with ``has_data_protection_clause = false``.

This cannot be fixed by editing the seed alone. ``seed_clause_master`` skips any
category that already exists (``if existing is not None: continue``), which is
correct - the Clause Master is administrator-editable and a re-seed must not
overwrite curated rules - but it means a seed change never reaches a database
that has been seeded once. Every deployment has been.

So the new vocabulary is applied here, as a new **version 2** rule, and only
where the active rule is still the untouched seeded baseline. An administrator
who has edited this rule through the UI keeps their edit and is left a log line;
overwriting their work to ship a keyword list would be the worse failure.

The old rule row is kept and deactivated rather than deleted: ``clauses`` rows
extracted under it carry its version in their provenance, and deleting it would
leave that pointing at nothing.

Applies to new extractions only. Existing contracts keep the clauses they were
extracted with until they are reprocessed from the extraction stage.

Revision ID: 0013
Revises: 0012
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

_CATEGORY_KEY = "data_protection"
_BASELINE_NOTE = "Seeded baseline rule."
_CHANGE_NOTE = "Added DPDP Act (India) headings, keywords and regulation value."

_HEADINGS = [
    "data protection",
    "data privacy",
    "personal data",
    "gdpr",
    "data processing",
    "digital personal data protection",
    "dpdp",
    "dpdpa",
]

_KEYWORDS = [
    "personal data",
    "data processor",
    "data controller",
    "gdpr",
    "processing of data",
    "data subject",
    "sub-processor",
    "data fiduciary",
    "data principal",
    "significant data fiduciary",
    "consent manager",
    "digital personal data",
]

_SYNONYMS = [
    "Privacy",
    "GDPR Compliance",
    "Data Processing",
    "Data Processing Addendum",
    "DPDP Compliance",
    "Digital Personal Data Protection Act",
]

_REGULATIONS_DESCRIPTION = (
    "Named regimes, e.g. GDPR, UK_GDPR, CCPA, HIPAA, DPDPA. Use DPDPA for India's "
    "Digital Personal Data Protection Act 2023, however the clause spells it."
)

_ROLE_DESCRIPTION = (
    "Our role: controller, processor, joint_controller, none. Under the DPDP Act "
    "read Data Fiduciary as controller and Data Processor as processor."
)


def _schema() -> str | None:
    from app.core.config import get_settings

    name = get_settings().db.schema_name.strip()
    return name or None


def _tables(schema: str | None) -> tuple[sa.TableClause, sa.TableClause]:
    """Lightweight table handles.

    Built with ``sa.table`` rather than interpolated SQL strings so the schema
    name is quoted by SQLAlchemy and the statements below are injection-free by
    construction.
    """
    categories = sa.table(
        "clause_master_categories",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("key", sa.String),
        schema=schema,
    )
    rules = sa.table(
        "clause_master_rules",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("category_id", postgresql.UUID(as_uuid=True)),
        sa.column("version", sa.Integer),
        sa.column("extraction_rule", postgresql.JSONB),
        sa.column("prompt_template_id", sa.String),
        sa.column("synonyms", postgresql.JSONB),
        sa.column("output_schema", postgresql.JSONB),
        sa.column("validation_rules", postgresql.JSONB),
        sa.column("is_active", sa.Boolean),
        sa.column("change_note", sa.String),
        schema=schema,
    )
    return categories, rules


def upgrade() -> None:
    bind = op.get_bind()
    categories, rules = _tables(_schema())

    row = bind.execute(
        sa.select(categories.c.id).where(categories.c.key == _CATEGORY_KEY)
    ).first()
    if row is None:
        # A database seeded before this category existed, or a partial fixture.
        # Nothing to upgrade; the seeder will create it with the new rule.
        print("0013: no data_protection category; skipping")
        return
    category_id = row[0]

    active = bind.execute(
        sa.select(
            rules.c.id, rules.c.version, rules.c.change_note, rules.c.output_schema
        )
        .where(rules.c.category_id == category_id, rules.c.is_active.is_(True))
        .order_by(rules.c.version.desc())
        .limit(1)
    ).first()
    if active is None:
        print("0013: data_protection has no active rule; skipping")
        return

    rule_id, version, change_note, output_schema = active

    if (change_note or "").strip() != _BASELINE_NOTE:
        # Someone has curated this rule. Leave it alone and say so.
        print(
            f"0013: data_protection rule v{version} was edited ({change_note!r}); "
            "leaving it untouched. Add the DPDP headings and keywords through "
            "Clause Master if you want them."
        )
        return

    # Carry the existing schema forward, replacing only the two descriptions that
    # name the regime. The rest - breach_notice_hours, transfer mechanism and so
    # on - is regime-neutral and stays exactly as it is.
    schema_json = dict(output_schema or {})
    properties = dict(schema_json.get("properties") or {})
    for field, description in (
        ("regulations", _REGULATIONS_DESCRIPTION),
        ("our_role", _ROLE_DESCRIPTION),
    ):
        if field in properties:
            updated = dict(properties[field])
            updated["description"] = description
            properties[field] = updated
    schema_json["properties"] = properties

    bind.execute(sa.update(rules).where(rules.c.id == rule_id).values(is_active=False))
    bind.execute(
        sa.insert(rules).values(
            category_id=category_id,
            version=int(version) + 1,
            extraction_rule={"headings": _HEADINGS, "keywords": _KEYWORDS},
            prompt_template_id="extraction.clauses",
            synonyms=_SYNONYMS,
            output_schema=schema_json,
            validation_rules={},
            is_active=True,
            change_note=_CHANGE_NOTE,
        )
    )
    print(f"0013: data_protection rule v{int(version) + 1} activated")


def downgrade() -> None:
    bind = op.get_bind()
    categories, rules = _tables(_schema())

    row = bind.execute(
        sa.select(categories.c.id).where(categories.c.key == _CATEGORY_KEY)
    ).first()
    if row is None:
        return
    category_id = row[0]

    # Only reverse our own row, identified by its change note - a rule an
    # administrator added afterwards must survive a downgrade.
    added = bind.execute(
        sa.select(rules.c.id, rules.c.version)
        .where(rules.c.category_id == category_id, rules.c.change_note == _CHANGE_NOTE)
        .order_by(rules.c.version.desc())
        .limit(1)
    ).first()
    if added is None:
        return

    bind.execute(sa.delete(rules).where(rules.c.id == added[0]))
    bind.execute(
        sa.update(rules)
        .where(rules.c.category_id == category_id, rules.c.version == int(added[1]) - 1)
        .values(is_active=True)
    )
