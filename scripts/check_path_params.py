"""Every declared path parameter must exist in the route's path.

A dependency that declares `contract_id: Annotated[UUID, Path(...)]` and is attached
to a route whose path has no `{contract_id}` placeholder produces a route that 422s
on every authenticated call. FastAPI does not reject this at startup, and the
OpenAPI schema happily documents a path parameter that cannot be supplied - so the
endpoint looks correct in the docs and is unusable in practice.

This is exactly the kind of failure a route-existence check misses: the path is
there, the method is there, and it still cannot succeed.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path as FsPath

sys.path.insert(0, str(FsPath(__file__).resolve().parents[1] / "backend"))

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://cip:cip@localhost:5432/cip")
os.environ.setdefault("JWT_SECRET", "x" * 48)
os.environ.setdefault("OTEL_ENABLED", "false")

PLACEHOLDER = re.compile(r"\{([^}:]+)")


def main() -> int:
    from fastapi.routing import APIRoute

    from app.main import create_app

    app = create_app()
    # Force the deferred `_IncludedRouter` entries to flatten.
    app.openapi()

    # Current FastAPI wraps each `include_router` call in a lazily-flattened
    # `_IncludedRouter`, so the concrete routes live under `effective_route_contexts`
    # rather than directly in `app.routes`.
    # `_EffectiveRouteContext` is not an `APIRoute` but carries the same `path` and
    # `dependant`, which is all this check needs.
    def walk(routes: list) -> list:
        found: list = []
        for route in routes:
            if isinstance(route, APIRoute):
                found.append(route)
                continue
            contexts = getattr(route, "effective_route_contexts", None)
            if callable(contexts):
                found.extend(contexts())
                continue
            nested = getattr(route, "routes", None)
            if nested:
                found.extend(walk(nested))
        return found

    routes = walk(app.routes)
    print(f"inspecting {len(routes)} API routes")

    def path_params(dependant) -> list[str]:
        names = [param.name for param in dependant.path_params]
        for sub in dependant.dependencies:
            names.extend(path_params(sub))
        return names

    failures: list[str] = []
    for route in routes:
        available = set(PLACEHOLDER.findall(route.path))
        declared = set(path_params(route.dependant))
        missing = sorted(declared - available)
        if missing:
            methods = ",".join(sorted(route.methods or []))
            failures.append(
                f"{methods} {route.path}: declares path param(s) "
                f"{', '.join(missing)} that the path does not contain"
            )

    if failures:
        print(
            f"\nFAIL: {len(failures)} route(s) declare an unsatisfiable path parameter"
        )
        for line in failures:
            print(f"  - {line}")
        return 1

    print("PASS: every declared path parameter is present in its route path")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
