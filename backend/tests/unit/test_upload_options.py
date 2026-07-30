"""The `use_enum_values` trap on upload options.

``BaseSchema`` sets ``use_enum_values=True``, so an enum-typed field on any request
schema holds a plain ``str`` after validation, not the enum member. Two things then
break in ways that do not look related:

* ``options.priority is not JobPriority.NORMAL`` is *always* true - identity against
  an enum member never holds for a string - so a "use the project default" branch
  guarded that way is dead code;
* ``options.priority.value`` raises ``AttributeError``, and because it sits after
  the file has already been stored, it turns a successful upload into a 500.

Both were live: every single upload returned 500. These tests pin the shape of the
data rather than the symptom, so the next enum field added to an upload option is
covered too.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.core.enums import AgreementType, JobPriority
from app.schemas.contract import UploadOptions
from app.services.upload import UploadService


class _Project:
    """Minimal stand-in: `_resolve_priority` only ever reads a setting."""

    def __init__(self, **settings: Any) -> None:
        self.id = uuid.uuid4()
        self._settings = settings

    def setting(self, key: str, default: Any = None) -> Any:
        return self._settings.get(key, default)


def _service() -> UploadService:
    # `_resolve_priority` touches no database state, so the session is never used.
    return UploadService(db=None)  # type: ignore[arg-type]


# =============================================================================
# The trap itself
# =============================================================================
def test_enum_fields_validate_to_plain_strings() -> None:
    """Pin the behaviour the rest of this module has to work around.

    If a future pydantic-settings change makes these real enum members again, this
    test fails loudly rather than letting the workarounds rot into no-ops.
    """
    options = UploadOptions(priority=JobPriority.HIGH, agreement_type=AgreementType.MSA)

    assert options.priority == JobPriority.HIGH
    assert not isinstance(options.priority, JobPriority)
    assert options.priority == "high"

    assert not isinstance(options.agreement_type, AgreementType)


def test_the_field_type_depends_on_how_the_model_was_built() -> None:
    """The nastiest part of this trap, and the reason it survived review.

    Pydantic does not validate defaults, so ``UploadOptions()`` keeps a real enum
    member while ``UploadOptions(priority=...)`` converts to a string. One field,
    two runtime types, decided by the caller.

    The upload route always passes the value explicitly - it has a ``Form()``
    default of ``JobPriority.NORMAL`` - so production only ever took the string
    branch, while a test constructing ``UploadOptions()`` would take the other one
    and see nothing wrong.
    """
    defaulted = UploadOptions().priority
    supplied = UploadOptions(priority=JobPriority.NORMAL).priority

    assert isinstance(defaulted, JobPriority)
    assert not isinstance(supplied, JobPriority)

    # Which is what made the identity check unreachable on the path that matters.
    assert defaulted is JobPriority.NORMAL
    assert supplied is not JobPriority.NORMAL
    assert supplied == JobPriority.NORMAL


# =============================================================================
# _resolve_priority
# =============================================================================
def test_resolved_priority_is_a_real_enum_member() -> None:
    """The caller reads `.value` off this. A str there is a 500 on every upload.

    Built the way the route builds it - explicitly - which is the path that broke.
    """
    resolved = _service()._resolve_priority(  # type: ignore[arg-type]
        _Project(),
        UploadOptions(priority=JobPriority.NORMAL),
    )

    assert isinstance(resolved, JobPriority)
    assert resolved.value == "normal"


def test_an_explicit_priority_wins() -> None:
    resolved = _service()._resolve_priority(  # type: ignore[arg-type]
        _Project(processing_priority="low"),
        UploadOptions(priority=JobPriority.HIGH),
    )
    assert resolved is JobPriority.HIGH


def test_the_project_default_is_actually_consulted() -> None:
    """The regression: an identity check returned early and never read this.

    Explicitly-constructed options, as the route builds them - with
    ``UploadOptions()`` the old code happened to work, which is why this went
    unnoticed.
    """
    resolved = _service()._resolve_priority(  # type: ignore[arg-type]
        _Project(processing_priority="low"),
        UploadOptions(priority=JobPriority.NORMAL),
    )
    assert resolved is JobPriority.LOW


def test_an_unreadable_project_default_falls_back_rather_than_raising() -> None:
    """A bad settings value must not fail the upload it is only decorating."""
    resolved = _service()._resolve_priority(  # type: ignore[arg-type]
        _Project(processing_priority="urgent-ish"),
        UploadOptions(),
    )
    assert resolved is JobPriority.NORMAL


def test_no_project_default_falls_back_to_normal() -> None:
    resolved = _service()._resolve_priority(_Project(), UploadOptions())  # type: ignore[arg-type]
    assert resolved is JobPriority.NORMAL


# =============================================================================
# The metric label that raised
# =============================================================================
def test_the_resolved_priority_supports_the_metric_label() -> None:
    """Reproduces the failing line: `metrics...labels(priority=priority.value)`."""
    resolved = _service()._resolve_priority(_Project(), UploadOptions())  # type: ignore[arg-type]
    assert resolved.value == "normal"


def test_agreement_type_survives_the_str_conversion() -> None:
    """`str(...)` rather than `.value`, and the stored value must still be the code."""
    options = UploadOptions(agreement_type=AgreementType.MSA)
    assert str(options.agreement_type) == AgreementType.MSA.value


def test_agreement_type_stays_none_when_not_supplied() -> None:
    assert UploadOptions().agreement_type is None


@pytest.mark.parametrize("priority", list(JobPriority))
def test_every_priority_round_trips(priority: JobPriority) -> None:
    resolved = _service()._resolve_priority(  # type: ignore[arg-type]
        _Project(),
        UploadOptions(priority=priority),
    )
    assert isinstance(resolved, JobPriority)
    assert resolved.value == priority.value
