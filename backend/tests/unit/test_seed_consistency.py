"""The seeded reference data has to agree with itself.

Two independent tables are joined by plain strings and validated by nothing:
`document_profiles.mandatory_clauses` / `optional_clauses` hold Clause Master
*keys*, and the Clause Master holds the categories those keys are supposed to
name. Nothing at seed time, at startup or at runtime checks that the join
resolves.

The cost of that is not a crash, which is why it survived. `AIExtractionStage`
filters the Clause Master down to the profile's key set
(`ai_extraction.py:279-287`), so a key naming nothing simply matches nothing: the
clause is never extracted, and because it is *mandatory* it is then reported as
permanently missing on every document of that type. The contract looks
non-compliant and no amount of re-processing fixes it.

That is exactly what `license_agreement` did. It listed `termination`, but the
Clause Master splits termination into `termination_for_cause` and
`termination_for_convenience` - there has never been a plain `termination`
category. Verified against the live database before it was fixed:

    agreement_type     | profile                        | orphan_mandatory_key
    ------------------+--------------------------------+---------------------
    license_agreement | License and Services Agreement | termination

These tests run over the seed definitions rather than a database, so they hold
for a deployment that has not been seeded yet and cost nothing to run.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.db.clause_seeds import ALL_CLAUSE_SEEDS, PRIORITY_CLAUSE_ORDER
from app.db.seed import PROFILE_SEEDS

#: Every Clause Master key the seed will create.
CLAUSE_KEYS: frozenset[str] = frozenset(str(seed.key) for seed in ALL_CLAUSE_SEEDS)


def _keys(profile: dict[str, Any], field: str) -> list[str]:
    return [str(key) for key in (profile.get(field) or [])]


def _profile_ids() -> list[str]:
    return [str(profile.get("key")) for profile in PROFILE_SEEDS]


@pytest.mark.parametrize("profile", PROFILE_SEEDS, ids=_profile_ids())
def test_every_mandatory_clause_key_names_a_real_category(profile: dict[str, Any]) -> None:
    """A mandatory key that resolves to nothing is reported missing forever."""
    orphans = sorted(set(_keys(profile, "mandatory_clauses")) - CLAUSE_KEYS)
    assert not orphans, (
        f"profile '{profile.get('key')}' marks {orphans} mandatory, but no Clause "
        f"Master category has that key. It can never be extracted and will be "
        f"reported missing on every document of this type."
    )


@pytest.mark.parametrize("profile", PROFILE_SEEDS, ids=_profile_ids())
def test_every_optional_clause_key_names_a_real_category(profile: dict[str, Any]) -> None:
    """Quieter than the mandatory case - the clause is just never looked for."""
    orphans = sorted(set(_keys(profile, "optional_clauses")) - CLAUSE_KEYS)
    assert not orphans, (
        f"profile '{profile.get('key')}' lists {orphans} as optional, but no Clause "
        f"Master category has that key."
    )


@pytest.mark.parametrize("profile", PROFILE_SEEDS, ids=_profile_ids())
def test_mandatory_and_optional_do_not_overlap(profile: dict[str, Any]) -> None:
    """A clause is required or it is not; both is a contradiction the UI cannot show."""
    overlap = sorted(
        set(_keys(profile, "mandatory_clauses")) & set(_keys(profile, "optional_clauses"))
    )
    assert not overlap, f"profile '{profile.get('key')}' lists {overlap} as both"


def test_clause_keys_are_unique() -> None:
    """The Clause Master is keyed on `key`; a duplicate would silently drop a seed."""
    assert len(PRIORITY_CLAUSE_ORDER) == len(set(PRIORITY_CLAUSE_ORDER))


def test_exactly_one_profile_is_the_default() -> None:
    """The fallback when no `agreement_type` matches. Two would make it arbitrary."""
    defaults = [str(p.get("key")) for p in PROFILE_SEEDS if p.get("is_default")]
    assert len(defaults) == 1, f"expected one default profile, found {defaults}"


def test_profile_agreement_types_are_unique() -> None:
    """`_resolve_profile` picks by `agreement_type`; duplicates make it order-dependent."""
    types = [str(p.get("agreement_type")) for p in PROFILE_SEEDS]
    duplicated = sorted({value for value in types if types.count(value) > 1})
    assert not duplicated, f"more than one profile claims {duplicated}"


def test_every_profile_names_at_least_one_mandatory_clause() -> None:
    """A profile with no mandatory clauses can never report one missing."""
    empty = [
        str(profile.get("key"))
        for profile in PROFILE_SEEDS
        if not _keys(profile, "mandatory_clauses")
    ]
    assert not empty, f"profiles with no mandatory clauses: {empty}"
