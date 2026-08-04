"""Regression tests for enum coercion in :meth:`app.export.service.ExportService.create`.

``ExportCreateRequest`` inherits ``BaseSchema``, which sets ``use_enum_values=True``.
So ``payload.export_format`` is the *string* ``"xlsx"``, not ``ExportFormat.XLSX``,
however the service signature is annotated - Pydantic has already unwrapped it and
no type checker sees the difference at the call site.

That broke ``POST /exports`` outright: ``create`` reads ``export_format.value`` for
the audit label, and a ``str`` has no ``.value``. Every export request answered 500
before a row was written, so the feature had never run end to end.

Worse, and quieter: ``create`` also guards with ``scope is SearchScope.PROJECT``.
An identity check against a plain string is always ``False``, so the two validation
branches below it - "a project-scoped export needs a project_id", "a contract-scoped
export needs a scope_ref" - could never fire. A malformed request would have been
accepted and queued, and failed later in a background task where nobody sees it.

These tests therefore pass **strings**, exactly as a real request does. Passing enum
members would exercise a path the API never takes and would have gone on passing
throughout the outage.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.core.enums import ExportFormat, ExportStatus, SearchScope
from app.core.errors import ValidationError
from app.export.service import ExportService


class StubSession:
    """The slice of AsyncSession ``create`` touches before it returns."""

    def __init__(self) -> None:
        self.added: list[Any] = []
        self.flushes = 0

    def add(self, obj: Any) -> None:
        if getattr(obj, "id", None) is None:
            obj.id = uuid.uuid4()
        self.added.append(obj)

    async def flush(self) -> None:
        self.flushes += 1

    async def execute(self, _statement: Any) -> Any:  # pragma: no cover - audit insert
        raise AssertionError("create() should not query")


@pytest.fixture
def service(monkeypatch: pytest.MonkeyPatch) -> ExportService:
    # The audit write is a separate concern with its own tests, and stubbing it
    # keeps these focused on the coercion.
    async def _record(*_args: Any, **_kwargs: Any) -> None:
        return None

    from app.services import audit as audit_module

    monkeypatch.setattr(audit_module.AuditService, "record", _record)
    return ExportService(StubSession())  # type: ignore[arg-type]


async def _create(service: ExportService, **overrides: Any) -> Any:
    kwargs: dict[str, Any] = {
        "user_id": uuid.uuid4(),
        "user_email": "someone@example.com",
        "project_id": uuid.uuid4(),
        # Strings, because that is what `use_enum_values` hands the endpoint.
        "scope": "project",
        "scope_ref": None,
        "export_format": "xlsx",
        "entities": ["contracts"],
        "filters": {},
        "fields": {},
    }
    kwargs.update(overrides)
    return await service.create(**kwargs)


@pytest.mark.asyncio
async def test_a_string_format_is_accepted(service: ExportService) -> None:
    """The exact call the endpoint makes. This raised AttributeError."""
    job = await _create(service)
    assert job.export_format is ExportFormat.XLSX
    assert job.status is ExportStatus.QUEUED


@pytest.mark.asyncio
async def test_a_string_scope_is_coerced_onto_the_row(service: ExportService) -> None:
    job = await _create(service, scope="application", project_id=None)
    assert job.scope is SearchScope.APPLICATION


@pytest.mark.asyncio
async def test_enum_members_still_work(service: ExportService) -> None:
    """Coercing must not break the callers that pass the real thing."""
    job = await _create(service, scope=SearchScope.PROJECT, export_format=ExportFormat.XLSX)
    assert job.export_format is ExportFormat.XLSX
    assert job.scope is SearchScope.PROJECT


@pytest.mark.asyncio
async def test_a_project_scope_without_a_project_is_rejected(service: ExportService) -> None:
    """The guard that `scope is SearchScope.PROJECT` silently skipped."""
    with pytest.raises(ValidationError):
        await _create(service, scope="project", project_id=None)


@pytest.mark.asyncio
async def test_a_contract_scope_without_a_reference_is_rejected(service: ExportService) -> None:
    with pytest.raises(ValidationError):
        await _create(service, scope="contract", scope_ref=None)


@pytest.mark.asyncio
async def test_an_unknown_format_is_rejected_before_anything_is_written(
    service: ExportService,
) -> None:
    """A misspelled format must not become a queued job that fails later."""
    with pytest.raises(ValueError):
        await _create(service, export_format="parquet")
    assert service.db.added == []  # type: ignore[attr-defined]
