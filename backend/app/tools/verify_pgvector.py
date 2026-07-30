"""Verify the database's vector store against the live embedding model.

    python -m app.tools.verify_pgvector
    python -m app.tools.verify_pgvector --json
    python -m app.tools.verify_pgvector --generate-migration

Reads the extension, every vector column and every vector index from the live
catalogue, then compares the embedding column against what the provider actually
returns. Nothing is inferred from the models or the migration history - a schema one
migration behind looks identical to a correct one from inside the application.

``--generate-migration`` writes the migration that would fix a mismatch and stops.
It never applies it: the migration destroys every stored vector, which is not
something a verification tool should do as a side effect.

Exit codes: 0 consistent, 1 a mismatch or a missing extension, 3 unreachable.
"""

from __future__ import annotations

import argparse
from typing import Any

from app.tools._common import (
    EXIT_FAILED,
    EXIT_OK,
    EXIT_UNUSABLE,
    base_parser,
    configure_tool_logging,
    dim,
    emit,
    heading,
    mark,
    run,
)


async def verify(args: argparse.Namespace) -> int:
    from sqlalchemy import text as sql

    from app.ai.embedding import migration_plan, pgvector
    from app.core.config import get_settings
    from app.db.session import session_scope, shutdown_engine

    configure_tool_logging(args.as_json)
    settings = get_settings().embedding

    report: dict[str, Any] = {
        "configured": {
            "provider": settings.provider,
            "model": settings.model,
            "dimension": settings.dim,
            "storage": settings.storage,
        },
        "pgvector": {},
        "reported_dimension": None,
        "problems": [],
        "warnings": [],
        "migration": None,
        "status": "unknown",
    }
    problems: list[str] = report["problems"]
    warnings: list[str] = report["warnings"]

    # The provider first: its answer is the ground truth the column is judged
    # against, and a configuration that disagrees with the model is itself the bug.
    reported: int | None = None
    if not args.skip_provider:
        from app.ai.embedding import get_embedding_provider

        try:
            probe = await get_embedding_provider().probe()
            if probe.ok:
                reported = probe.dim
                report["reported_dimension"] = probe.dim
            else:
                warnings.append(
                    f"The embedding provider did not answer ({probe.error}). Falling "
                    "back to EMBEDDING_DIM for the comparison."
                )
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"The embedding provider could not be probed: {exc}")

    existing_vectors: int | None = None
    try:
        async with session_scope() as db:
            info = await pgvector.inspect(db)
            if info.installed:
                try:
                    existing_vectors = int(
                        (await db.execute(sql("SELECT count(*) FROM embeddings"))).scalar_one()
                    )
                except Exception:  # noqa: BLE001 - table may not exist yet
                    existing_vectors = None
    except Exception as exc:  # noqa: BLE001
        report["status"] = "unreachable"
        problems.append(f"Could not connect to PostgreSQL: {exc}")
        emit(report, lambda: _render(report, None, None), as_json=args.as_json)
        await shutdown_engine()
        return EXIT_UNUSABLE
    finally:
        await shutdown_engine()

    report["pgvector"] = info.as_dict()
    report["existing_vectors"] = existing_vectors

    if not info.reachable:
        problems.append(f"PostgreSQL is unreachable: {info.error}")
    elif not info.installed:
        problems.append(
            "The pgvector extension is not installed. Every vector column and every "
            "similarity query depends on it. Run `CREATE EXTENSION vector`."
        )
    else:
        if settings.storage == "halfvec" and not info.supports_halfvec:
            problems.append(
                f"pgvector {info.version} does not support halfvec (needs >= 0.7.0), "
                f"but EMBEDDING_STORAGE=halfvec. Upgrade the extension, or use "
                f"EMBEDDING_STORAGE=vector with EMBEDDING_DIM <= 2000."
            )
        if not info.columns:
            warnings.append("No vector columns found. Has the initial migration run?")

        expected = reported or settings.dim
        for column in info.columns:
            if column.table == migration_plan.TARGET_TABLE:
                continue
            if column.dim is not None and column.dim != expected:
                warnings.append(
                    f"{column.qualified} is {column.type_name}({column.dim}), which "
                    f"does not match the active model's {expected}."
                )

        invalid = [index for index in info.indexes if not index.valid]
        for index in invalid:
            problems.append(
                f"Index {index.name} on {index.table} is INVALID. The planner will not "
                "use it, so every similarity search over that table is a sequential "
                "scan. Drop and recreate it."
            )

    plan = migration_plan.detect(info, reported_dim=reported, existing_vectors=existing_vectors)
    report["migration"] = plan.as_dict()
    if plan.required:
        problems.extend(plan.reasons)

    if args.generate_migration and plan.required:
        path = migration_plan.write(plan)
        report["migration"] = plan.as_dict()
        report["migration"]["generated_path"] = str(path)

    report["status"] = "ok" if not problems else "mismatch"
    emit(report, lambda: _render(report, info, plan), as_json=args.as_json)
    return EXIT_OK if not problems else EXIT_FAILED


def _render(report: dict[str, Any], info: Any, plan: Any) -> None:
    configured = report["configured"]
    print(heading("pgvector verification"))
    print(f"  {'Provider':<20} {configured['provider']}")
    print(f"  {'Model':<20} {configured['model']}")
    print(f"  {'Configured dim':<20} {configured['dimension']}")
    if report["reported_dimension"]:
        print(f"  {'Model reports':<20} {report['reported_dimension']}")
    print(f"  {'Storage':<20} {configured['storage']}")

    if info is not None:
        print(heading("Extension"))
        print(f"  {mark(info.installed)} pgvector {info.version or '(not installed)'}")
        if info.installed:
            print(f"  {mark(info.supports_halfvec)} halfvec support {dim('(requires >= 0.7.0)')}")

        if info.columns:
            print(heading("Vector columns"))
            for column in info.columns:
                expected = report["reported_dimension"] or configured["dimension"]
                ok = column.dim == expected and column.type_name == configured["storage"]
                print(f"  {mark(ok)} {column.qualified:<34} {column.type_name}({column.dim})")

        if info.indexes:
            print(heading("Vector indexes"))
            for index in info.indexes:
                print(f"  {mark(index.valid)} {index.name:<40} {index.method}")

    if report.get("existing_vectors") is not None:
        print(f"\n  {'Stored vectors':<20} {report['existing_vectors']:,}")

    for warning in report["warnings"]:
        print(f"\n{mark(None)} {warning}")

    if plan is not None and plan.required:
        print(heading("Migration"))
        print(plan.explain())
        if plan.path:
            print(f"\n{mark(True)} Migration written to {plan.path}")
            print("      Review it, then apply with `cip migrate`. Nothing has changed yet.")
        else:
            print(
                "\n      Re-run with --generate-migration to write the migration script. "
                "\n      Nothing is applied automatically."
            )
    elif not report["problems"]:
        print(f"\n{mark(True)} The vector store matches the active embedding model.")

    for problem in report["problems"]:
        if plan is None or problem not in getattr(plan, "reasons", []):
            print(f"\n{mark(False)} {problem}")


def build_parser() -> argparse.ArgumentParser:
    parser = base_parser(__doc__ or "")
    parser.add_argument(
        "--generate-migration",
        action="store_true",
        help="Write the migration that would fix a mismatch. Does not apply it.",
    )
    parser.add_argument(
        "--skip-provider",
        action="store_true",
        help="Compare against EMBEDDING_DIM instead of probing the provider.",
    )
    return parser


def main() -> int:
    return run(verify, build_parser().parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
