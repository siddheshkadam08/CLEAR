"""Whole-system diagnostic report.

    python -m app.tools.system_diagnostics
    python -m app.tools.system_diagnostics --json
    python -m app.tools.system_diagnostics --markdown EMBEDDING_AUDIT.md

Collects everything the other tools check, plus dependency and configuration state,
into one report that can be attached to a deployment ticket. Read-only.

Exit codes: 0 healthy, 1 something is broken, 2 warnings only (with --strict).
"""

from __future__ import annotations

import argparse
import platform
import sys
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from pathlib import Path
from typing import Any

from app.tools._common import (
    EXIT_FAILED,
    EXIT_OK,
    EXIT_WARNING,
    base_parser,
    configure_tool_logging,
    emit,
    heading,
    mark,
    run,
)

#: Optional provider packages. Absence is reported, never treated as an error - the
#: platform is built to run without any of them.
OPTIONAL_PACKAGES = (
    ("anthropic", "Claude inference"),
    ("openai", "OpenAI / Azure OpenAI inference and embeddings"),
    ("sentence_transformers", "local embeddings"),
    ("fitz", "PyMuPDF parsing (package: pymupdf)"),
    ("xlsxwriter", "XLSX export"),
    ("pgvector", "vector column types"),
)

REQUIRED_PACKAGES = ("fastapi", "sqlalchemy", "httpx", "pydantic", "alembic")


async def collect(args: argparse.Namespace) -> dict[str, Any]:
    from app.ai.embedding import migration_plan, pgvector
    from app.ai.embedding.diagnostics import diagnose
    from app.core.config import get_settings

    settings = get_settings()
    embedding = settings.embedding

    report: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "environment": {
            "app_env": settings.app_env,
            "python": platform.python_version(),
            "platform": platform.platform(),
        },
        "embedding": {
            "provider": embedding.provider,
            "model": embedding.model,
            "configured_dimension": embedding.dim,
            "native_dimension": embedding.native_dim,
            "storage": embedding.storage,
            "endpoint": embedding.nvidia_base_url if embedding.provider == "nvidia" else None,
            "authenticated": bool(embedding.nvidia_api_key)
            if embedding.provider == "nvidia"
            else None,
            "batch_size": embedding.batch_size,
        },
        "llm": {"provider": settings.llm.provider, "model": settings.llm.model},
        "parser": {"active": settings.parser.active_parser},
        "packages": {"required": {}, "optional": {}},
        "database": {},
        "pgvector": {},
        "migration": {},
        "startup_validation": {},
        "health": {},
        "warnings": [],
        "problems": [],
    }

    # --- packages -----------------------------------------------------------
    for name in REQUIRED_PACKAGES:
        report["packages"]["required"][name] = _version(name)
    for name, purpose in OPTIONAL_PACKAGES:
        installed = _version(name)
        report["packages"]["optional"][name] = {"version": installed, "purpose": purpose}

    missing_required = [n for n, v in report["packages"]["required"].items() if v is None]
    if missing_required:
        report["problems"].append(
            f"Required package(s) not installed: {', '.join(missing_required)}."
        )

    if embedding.provider in {"openai", "azure_openai"} and not _version("openai"):
        report["problems"].append(
            "EMBEDDING_PROVIDER is an OpenAI variant but the `openai` package is not "
            "installed. Install the `ai` extra."
        )
    if settings.llm.provider == "anthropic" and not _version("anthropic"):
        report["problems"].append(
            "LLM_PROVIDER=anthropic but the `anthropic` package is not installed. "
            "Install the `ai` extra."
        )

    # --- configuration + provider ------------------------------------------
    startup = await diagnose(None, probe_provider=not args.skip_provider, settings=settings)
    report["startup_validation"] = startup.as_dict()
    report["embedding"]["reported_dimension"] = startup.reported_dim
    report["embedding"]["latency_ms"] = startup.latency_ms
    for finding in startup.findings:
        if finding.ok:
            continue
        (report["problems"] if finding.fatal else report["warnings"]).append(
            f"{finding.check}: {finding.detail}"
        )

    # --- database -----------------------------------------------------------
    try:
        from app.db.session import session_scope, shutdown_engine

        try:
            async with session_scope() as db:
                info = await pgvector.inspect(db)
                report["database"] = {"reachable": True}
                report["pgvector"] = info.as_dict()

                plan = migration_plan.detect(info, reported_dim=startup.reported_dim)
                report["migration"] = plan.as_dict()
                if plan.required:
                    report["problems"].extend(plan.reasons)

                if (
                    info.installed
                    and settings.embedding.storage == "halfvec"
                    and not info.supports_halfvec
                ):
                    report["problems"].append(
                        f"pgvector {info.version} predates halfvec (>= 0.7.0)."
                    )
                for index in info.indexes:
                    if not index.valid:
                        report["problems"].append(
                            f"Index {index.name} is INVALID; searches on {index.table} "
                            "are sequential scans."
                        )
        finally:
            await shutdown_engine()
    except Exception as exc:  # noqa: BLE001 - an unreachable DB is a finding
        report["database"] = {"reachable": False, "error": str(exc)[:300]}
        report["warnings"].append(
            f"PostgreSQL is unreachable, so schema checks were skipped: {exc}"
        )

    # --- health -------------------------------------------------------------
    try:
        from app.api.health import _embedding_health

        report["health"] = await _embedding_health()
    except Exception as exc:  # noqa: BLE001
        report["health"] = {"status": "error", "detail": str(exc)[:200]}

    report["status"] = (
        "unhealthy" if report["problems"] else ("degraded" if report["warnings"] else "healthy")
    )
    return report


def _version(name: str) -> str | None:
    try:
        return package_version(name if name != "fitz" else "pymupdf")
    except PackageNotFoundError:
        return None
    except Exception:  # noqa: BLE001 - never let metadata break a diagnostic
        return None


# =============================================================================
# Rendering
# =============================================================================
def render_text(report: dict[str, Any]) -> None:
    embedding = report["embedding"]
    print(heading("System diagnostics"))
    print(f"  Generated   {report['generated_at']}")
    print(
        f"  Environment {report['environment']['app_env']} "
        f"(python {report['environment']['python']})"
    )
    print(f"  Status      {report['status'].upper()}")

    print(heading("Embedding"))
    for label, value in (
        ("Provider", embedding["provider"]),
        ("Model", embedding["model"]),
        ("Endpoint", embedding["endpoint"] or "(local)"),
        ("Authenticated", _tri(embedding["authenticated"])),
        ("Configured dim", embedding["configured_dimension"]),
        ("Reported dim", embedding.get("reported_dimension") or "(not probed)"),
        ("Native dim", embedding["native_dimension"]),
        ("Storage", embedding["storage"]),
        ("Latency", f"{embedding.get('latency_ms')} ms" if embedding.get("latency_ms") else "-"),
    ):
        print(f"  {label:<16} {value}")

    pgv = report.get("pgvector") or {}
    print(heading("pgvector"))
    if not report.get("database", {}).get("reachable"):
        print(f"  {mark(None)} database unreachable")
    else:
        print(f"  {mark(pgv.get('installed'))} extension {pgv.get('version') or '(absent)'}")
        for column in pgv.get("columns", []):
            print(
                f"  {mark(True)} {column['table']}.{column['column']:<22} "
                f"{column['type']}({column['dimension']})"
            )
        for index in pgv.get("indexes", []):
            print(f"  {mark(index['valid'])} {index['name']:<38} {index['method']}")

    print(heading("Packages"))
    for name, ver in report["packages"]["required"].items():
        print(f"  {mark(ver is not None)} {name:<24} {ver or 'MISSING'}")
    for name, meta in report["packages"]["optional"].items():
        ver = meta["version"]
        print(
            f"  {mark(None) if ver is None else mark(True)} {name:<24} "
            f"{ver or 'not installed'}  ({meta['purpose']})"
        )

    migration = report.get("migration") or {}
    if migration.get("required"):
        print(heading("Migration"))
        print(f"  Current : {migration.get('current')}")
        print(f"  Target  : {migration.get('target')}")
        for reason in migration.get("reasons", []):
            print(f"  - {reason}")
        print("\n  Run `python -m app.tools.verify_pgvector --generate-migration` to")
        print("  write it. Nothing is applied automatically.")

    if report["problems"]:
        print(heading("Problems"))
        for problem in report["problems"]:
            print(f"  {mark(False)} {problem}")
    if report["warnings"]:
        print(heading("Warnings"))
        for warning in report["warnings"]:
            print(f"  {mark(None)} {warning}")
    if not report["problems"] and not report["warnings"]:
        print(f"\n{mark(True)} No problems detected.")


def render_markdown(report: dict[str, Any]) -> str:
    embedding = report["embedding"]
    pgv = report.get("pgvector") or {}
    migration = report.get("migration") or {}
    badge = {"healthy": "PASS", "degraded": "WARN", "unhealthy": "FAIL"}[report["status"]]

    lines = [
        "# Embedding system diagnostics",
        "",
        f"**Status: {badge}** — generated {report['generated_at']}",
        "",
        "## Embedding",
        "",
        "| | |",
        "| --- | --- |",
        f"| Provider | `{embedding['provider']}` |",
        f"| Model | `{embedding['model']}` |",
        f"| Endpoint | `{embedding['endpoint'] or '(local)'}` |",
        f"| Authenticated | {_tri(embedding['authenticated'])} |",
        f"| Configured dimension | {embedding['configured_dimension']} |",
        f"| Dimension reported by the model | {embedding.get('reported_dimension') or '_not probed_'} |",
        f"| Model native dimension | {embedding['native_dimension']} |",
        f"| Storage type | `{embedding['storage']}` |",
        f"| Latency | {embedding.get('latency_ms') or '—'} ms |",
        "",
        "## pgvector",
        "",
    ]

    if not report.get("database", {}).get("reachable"):
        lines += ["_Database unreachable; schema checks skipped._", ""]
    else:
        lines += [
            f"Extension: `{pgv.get('version') or 'not installed'}` "
            f"(halfvec support: {'yes' if pgv.get('supports_halfvec') else 'no'})",
            "",
            "| Table | Column | Type | Dimension |",
            "| --- | --- | --- | --- |",
        ]
        for column in pgv.get("columns", []):
            lines.append(
                f"| `{column['table']}` | `{column['column']}` | "
                f"`{column['type']}` | {column['dimension']} |"
            )
        if pgv.get("indexes"):
            lines += ["", "| Index | Method | Valid |", "| --- | --- | --- |"]
            for index in pgv["indexes"]:
                lines.append(
                    f"| `{index['name']}` | {index['method']} | "
                    f"{'yes' if index['valid'] else '**NO**'} |"
                )
        lines.append("")

    lines += ["## Migration", ""]
    if migration.get("required"):
        lines += [
            f"**Required.** `{migration.get('current')}` → `{migration.get('target')}`",
            "",
        ]
        lines += [f"- {reason}" for reason in migration.get("reasons", [])]
        lines += [
            "",
            "Indexes rebuilt: " + ", ".join(f"`{n}`" for n in migration.get("indexes_rebuilt", [])),
            "",
        ]
        if migration.get("destroys_vectors"):
            lines += [
                "> All embeddings must be regenerated. Vectors of different widths from",
                "> different models cannot be converted, and a mixed index returns",
                "> confident nonsense rather than failing. Run `cip reindex-embeddings",
                "> --all` after applying.",
                "",
            ]
    else:
        lines += ["Not required — the vector store matches the active model.", ""]

    lines += ["## Startup validation", "", "| Check | Result | Detail |", "| --- | --- | --- |"]
    for finding in report.get("startup_validation", {}).get("findings", []):
        result = "pass" if finding["ok"] else ("**FAIL**" if finding["fatal"] else "warn")
        lines.append(f"| `{finding['check']}` | {result} | {finding['detail']} |")
    lines.append("")

    lines += ["## Health endpoint", "", "```json", _json(report.get("health", {})), "```", ""]

    lines += [
        "## Installed AI providers",
        "",
        "| Package | Version | Purpose |",
        "| --- | --- | --- |",
    ]
    for name, meta in report["packages"]["optional"].items():
        lines.append(f"| `{name}` | {meta['version'] or '_not installed_'} | {meta['purpose']} |")
    lines.append("")

    if report["problems"]:
        lines += ["## Problems", ""] + [f"- {p}" for p in report["problems"]] + [""]
    if report["warnings"]:
        lines += ["## Warnings", ""] + [f"- {w}" for w in report["warnings"]] + [""]
    if not report["problems"] and not report["warnings"]:
        lines += ["## Problems", "", "None detected.", ""]

    return "\n".join(lines)


def _json(payload: Any) -> str:
    import json

    return json.dumps(payload, indent=2, default=str)


def _tri(value: bool | None) -> str:
    if value is None:
        return "n/a"
    return "yes" if value else "no"


async def diagnostics(args: argparse.Namespace) -> int:
    configure_tool_logging(args.as_json)
    report = await collect(args)

    if args.markdown:
        path = Path(args.markdown)
        # One small synchronous write at the end of a CLI run. Reaching for an
        # async filesystem library here would add a dependency to avoid blocking
        # an event loop that is about to exit.
        path.write_text(render_markdown(report), encoding="utf-8")  # noqa: ASYNC240
        if not args.as_json:
            print(f"{mark(True)} Markdown report written to {path}")

    emit(report, lambda: render_text(report), as_json=args.as_json)

    if report["problems"]:
        return EXIT_FAILED
    if report["warnings"] and args.strict:
        return EXIT_WARNING
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = base_parser(__doc__ or "")
    parser.add_argument("--markdown", metavar="PATH", help="Also write a Markdown report.")
    parser.add_argument("--strict", action="store_true", help="Exit non-zero on warnings.")
    parser.add_argument(
        "--skip-provider", action="store_true", help="Do not call the embedding provider."
    )
    return parser


def main() -> int:
    return run(diagnostics, build_parser().parse_args())


if __name__ == "__main__":
    sys.exit(main())
