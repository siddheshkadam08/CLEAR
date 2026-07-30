"""Verify every endpoint the frontend calls exists in the backend route table.

Parses `frontend/src/api/endpoints.ts` rather than restating its URLs here: a check
that duplicates the thing it checks passes whenever both copies are wrong together.

A frontend calling a path the backend does not serve is a 404 that appears only when
a user reaches that screen, which is exactly the class of breakage that survives a
green typecheck and a green build.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://cip:cip@localhost:5432/cip")
os.environ.setdefault("JWT_SECRET", "x" * 48)
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("STORAGE_PROVIDER", "local")
os.environ.setdefault("LLM_PROVIDER", "mock")
os.environ.setdefault("EMBEDDING_PROVIDER", "mock")
os.environ.setdefault("OTEL_ENABLED", "false")

TS = (ROOT / "frontend" / "src" / "api" / "endpoints.ts").read_text(encoding="utf-8")

# The call site, not its arguments: `api.get<Paginated<ProjectSummary>>(...)`.
# A `<[^>]*>` generic matcher silently skips every nested generic, which is most of
# this file - so the type argument is skipped by scanning to the opening paren.
CALL = re.compile(r"\b(api\.(?:get|post|patch|put|delete)|apiUpload|apiStream)\b")

LITERAL = re.compile(r"[`'\"](/[^`'\"]*)[`'\"]")


def normalise(path: str) -> str:
    """`/projects/${id}/contracts` -> `/projects/{}/contracts`."""
    return re.sub(r"\$\{[^}]*\}", "{}", path)


def route_pattern(path: str) -> str:
    """FastAPI `/projects/{project_id}/contracts` -> `/projects/{}/contracts`."""
    return re.sub(r"\{[^}]*\}", "{}", path)


def first_argument(text: str, open_paren: int) -> str:
    """Source of the first call argument, so a ternary path is captured whole.

    `contracts.list` picks its path with a conditional - project-scoped or
    cross-project - and only checking the first literal would leave the other
    branch unverified.
    """
    depth = 0
    for index in range(open_paren, len(text)):
        char = text[index]
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
            if depth == 0:
                return text[open_paren + 1 : index]
        elif char == "," and depth == 1:
            return text[open_paren + 1 : index]
    return ""


def collect_frontend_calls() -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []
    for match in CALL.finditer(TS):
        callee = match.group(1)
        method = (
            "POST"
            if callee in {"apiUpload", "apiStream"}
            else callee.split(".", 1)[1].upper()
        )
        paren = TS.find("(", match.end())
        if paren < 0:
            continue
        argument = first_argument(TS, paren)
        for literal in LITERAL.finditer(argument):
            calls.append((method, normalise(literal.group(1))))
    return calls


async def main() -> int:
    from app.main import create_app

    app = create_app()

    # The OpenAPI schema rather than `app.routes`: current FastAPI leaves deferred
    # `_IncludedRouter` entries in `.routes` and only flattens them on demand, so
    # walking that list silently sees almost nothing. Generating the schema also
    # proves the schema itself builds, which is the other thing that can break the
    # contract without breaking a test.
    schema = app.openapi()
    paths: dict = schema.get("paths", {})

    backend: set[tuple[str, str]] = set()
    for path, operations in paths.items():
        for method in operations:
            if method.upper() in {"HEAD", "OPTIONS", "PARAMETERS"}:
                continue
            backend.add((method.upper(), route_pattern(path)))

    api_v1 = sorted(p for p in paths if p.startswith("/api/v1"))
    print(
        f"backend routes: {len(backend)} method+path pairs, {len(api_v1)} paths under /api/v1"
    )

    calls = collect_frontend_calls()
    print(f"frontend calls: {len(calls)} parsed from endpoints.ts")
    if not calls:
        print("FAIL: parsed nothing - the regex no longer matches the file")
        return 1

    # The client prepends VITE_API_BASE_URL, which is `/api/v1` in every shipped
    # configuration (compose passes it explicitly; the client defaults to it).
    missing: list[tuple[str, str]] = []
    for method, path in sorted(set(calls)):
        if (method, route_pattern("/api/v1" + path)) not in backend:
            missing.append((method, path))

    for method, path in sorted(set(calls)):
        mark = "MISSING" if (method, path) in missing else "ok"
        print(f"  [{mark:>7}] {method:6} /api/v1{path}")

    # The refresh call is built by hand inside client.ts, not via endpoints.ts.
    extra = [("POST", "/auth/refresh")]
    for method, path in extra:
        if (method, route_pattern("/api/v1" + path)) not in backend:
            missing.append((method, path))
            print(f"  [MISSING] {method:6} /api/v1{path}  (from client.ts)")
        else:
            print(f"  [     ok] {method:6} /api/v1{path}  (from client.ts)")

    if missing:
        print(f"\nFAIL: {len(missing)} frontend call(s) have no backend route")
        return 1

    print("\nPASS: every frontend endpoint resolves to a backend route")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
