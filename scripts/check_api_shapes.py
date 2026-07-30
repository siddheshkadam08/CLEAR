"""Verify the frontend's hand-written types match the backend's response schemas.

`types.ts` is written by hand, so a renamed backend field produces a frontend that
compiles, builds, lints, and then renders `undefined` at runtime. Typecheck cannot
catch it: TypeScript is checking the hand-written type against itself.

This compares each frontend interface's field names against the corresponding
OpenAPI component schema. A field the frontend reads that the backend never sends
is the failure that matters; the reverse (backend sends more than the UI uses) is
reported but not fatal.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://cip:cip@localhost:5432/cip")
os.environ.setdefault("JWT_SECRET", "x" * 48)
os.environ.setdefault("OTEL_ENABLED", "false")

TS = (ROOT / "frontend" / "src" / "api" / "types.ts").read_text(encoding="utf-8")

#: frontend interface -> backend OpenAPI component schema.
PAIRS = {
    "TokenResponse": "TokenResponse",
    "CurrentUser": "CurrentUser",
    "ProjectMembership": "ProjectMembershipInfo",
    "ProjectListItem": "ProjectListItem",
    "ProjectDetail": "ProjectResponse",
    "ProjectStats": "ProjectStats",
    "ProjectRef": "ProjectRef",
    "ContractListItem": "ContractListItem",
    "ContractDetail": "ContractResponse",
    "ContractMetadata": "ContractMetadataResponse",
    "FileAccess": "FileAccessResponse",
    "UploadResult": "UploadResponse",
    "UploadedFileResult": "UploadedFileResult",
    "Clause": "ClauseResponse",
    "ClauseTab": "ClauseTabGroup",
    "Party": "EntityResponse",
    "Obligation": "ObligationResponse",
    "Risk": "RiskResponse",
    "KeyDate": "KeyDateResponse",
    "RiskAssessment": "RiskAssessmentResponse",
    "ContractKnowledge": "ContractKnowledgeResponse",
    "EvidenceResolution": "EvidenceResponse",
    "SearchHit": "SearchHit",
    "SearchResponse": "SearchResponse",
    "ContractMatch": "ContractMatch",
    "Citation": "CitationResponse",
    "AnswerResponse": "AnswerResponse",
    "ChatSession": "ChatSessionResponse",
    "ChatMessage": "ChatMessageResponse",
    "StageRun": "StageRunResponse",
    "JobListItem": "JobListItem",
    "Job": "JobResponse",
    "PipelineHealth": "PipelineHealthResponse",
    "KpiTile": "KpiTile",
    "DistributionBucket": "DistributionBucket",
    "Dashboard": "DashboardResponse",
    "ClauseCategory": "ClauseCategoryResponse",
    "Alert": "AlertResponse",
    "PlanExplanation": "PlanExplanation",
    "BoundingBox": "BoundingBox",
    "Provenance": "ProvenanceInfo",
    "PageMeta": "PageMeta",
    "ExportJob": "ExportResponse",
    "ExportDownload": "ExportDownloadResponse",
    "ExportCapabilities": "ExportCapabilitiesResponse",
}

INTERFACE = re.compile(
    r"export interface (\w+)(?:\s+extends\s+(\w+))?\s*\{(.*?)\n\}", re.DOTALL
)
FIELD = re.compile(r"^\s{2}(\w+)\??:", re.MULTILINE)


def frontend_interfaces() -> dict[str, tuple[str | None, set[str]]]:
    found: dict[str, tuple[str | None, set[str]]] = {}
    for match in INTERFACE.finditer(TS):
        name, parent, body = match.group(1), match.group(2), match.group(3)
        found[name] = (parent, set(FIELD.findall(body)))
    return found


def resolve(name: str, interfaces: dict[str, tuple[str | None, set[str]]]) -> set[str]:
    """Fields including anything inherited via `extends`."""
    parent, fields = interfaces[name]
    if parent and parent in interfaces:
        return fields | resolve(parent, interfaces)
    return fields


def schema_fields(schemas: dict, name: str) -> set[str] | None:
    schema = schemas.get(name)
    if schema is None:
        return None
    fields: set[str] = set(schema.get("properties", {}))
    # Pydantic renders inheritance as allOf when a model adds to a base.
    for part in schema.get("allOf", []):
        ref = part.get("$ref", "")
        if ref.startswith("#/components/schemas/"):
            nested = schema_fields(schemas, ref.rsplit("/", 1)[1])
            if nested:
                fields |= nested
        fields |= set(part.get("properties", {}))
    return fields


def main() -> int:
    from app.main import create_app

    schemas = create_app().openapi()["components"]["schemas"]
    interfaces = frontend_interfaces()

    print(f"backend component schemas: {len(schemas)}")
    print(f"frontend interfaces:       {len(interfaces)}")
    print()

    failures: list[str] = []
    unmapped: list[str] = []

    for ts_name, py_name in sorted(PAIRS.items()):
        if ts_name not in interfaces:
            failures.append(f"{ts_name}: no such interface in types.ts")
            continue

        backend_fields = schema_fields(schemas, py_name)
        if backend_fields is None:
            unmapped.append(f"{ts_name} -> {py_name} (no such backend schema)")
            continue

        frontend_fields = resolve(ts_name, interfaces)
        phantom = sorted(frontend_fields - backend_fields)
        unused = sorted(backend_fields - frontend_fields)

        if phantom:
            failures.append(
                f"{ts_name}: reads field(s) the backend never sends: {', '.join(phantom)}"
            )
            status = "FAIL"
        else:
            status = " ok "

        note = f"  (+{len(unused)} backend fields unused)" if unused else ""
        print(
            f"  [{status}] {ts_name:22} -> {py_name:30} {len(frontend_fields):2} fields{note}"
        )

    if unmapped:
        print("\nUnmapped (check the schema name):")
        for line in unmapped:
            print(f"  - {line}")

    if failures:
        print(f"\nFAIL: {len(failures)} shape mismatch(es)")
        for line in failures:
            print(f"  - {line}")
        return 1

    print("\nPASS: every mapped frontend type is a subset of its backend schema")
    return 0 if not unmapped else 1


if __name__ == "__main__":
    raise SystemExit(main())
