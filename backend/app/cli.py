"""Operator CLI (``python -m app.cli``, or ``cip`` once installed).

The entry point docker-compose and the Makefile drive: ``migrate`` runs on the
``migrate`` service before the API starts, ``scheduler`` is the background service
that reclaims stalled jobs and fires alert sweeps.

Every command follows the same two rules:

* **Exit codes are meaningful.** Non-zero on failure, so a compose healthcheck or a
  CI step can depend on the result rather than grepping output. A migration that
  half-applies must not let the API container start.
* **Nothing is destructive without saying so.** Commands that write announce what
  they are about to do and report what they changed.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path
from typing import Any

import typer

from app.core.logging import configure_logging, get_logger

app = typer.Typer(
    name="cip",
    help="Contract Intelligence Platform - operator commands.",
    no_args_is_help=True,
    add_completion=False,
)

logger = get_logger(__name__)

#: Repository root, so Alembic is found regardless of the working directory the
#: container happens to start in.
_BACKEND_ROOT = Path(__file__).resolve().parent.parent


def _alembic_config() -> Any:
    """Alembic config pinned to this package's ``alembic.ini``."""
    from alembic.config import Config

    config = Config(str(_BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_BACKEND_ROOT / "migrations"))
    return config


def _echo(message: str, *, err: bool = False) -> None:
    typer.echo(message, err=err)


def _fail(message: str, code: int = 1) -> None:
    """Report and exit non-zero.

    A failed command must be visible to the shell, not only to the log stream: the
    compose ``migrate`` service gates the API on this exit code.
    """
    _echo(f"ERROR: {message}", err=True)
    raise typer.Exit(code)


# =============================================================================
# Schema
# =============================================================================
@app.command()
def migrate(
    seed: bool = typer.Option(False, "--seed", help="Seed reference data after migrating."),
    revision: str = typer.Option("head", help="Target revision."),
) -> None:
    """Apply database migrations.

    Runs before the API in compose. Exits non-zero on failure so a broken migration
    stops the deployment rather than leaving the API to fail against a half-built
    schema.
    """
    configure_logging()
    from alembic import command

    _echo(f"Migrating to {revision}...")
    try:
        command.upgrade(_alembic_config(), revision)
    except Exception as exc:  # noqa: BLE001 - the message is the deliverable here
        _fail(f"Migration failed: {exc}")

    _echo("Migration complete.")
    if seed:
        _run(_seed_all())


@app.command()
def downgrade(
    revision: str = typer.Argument(..., help="Target revision, e.g. -1 or a hash."),
    yes: bool = typer.Option(False, "--yes", help="Skip the confirmation prompt."),
) -> None:
    """Roll back migrations.

    Confirmation is required by default: a downgrade drops tables, and the data in
    them does not come back.
    """
    configure_logging()
    from alembic import command

    if not yes:
        typer.confirm(
            f"Downgrade to {revision}? This drops schema objects and their data.",
            abort=True,
        )
    try:
        command.downgrade(_alembic_config(), revision)
    except Exception as exc:  # noqa: BLE001
        _fail(f"Downgrade failed: {exc}")
    _echo(f"Downgraded to {revision}.")


@app.command("current")
def current_revision() -> None:
    """Show the applied migration revision."""
    configure_logging()
    from alembic import command

    command.current(_alembic_config(), verbose=True)


# =============================================================================
# Seed
# =============================================================================
@app.command()
def seed() -> None:
    """Seed roles, the admin user, the Clause Master, profiles and alert rules.

    Idempotent: safe to run on every deploy. Existing rows are left alone rather
    than overwritten, so an administrator's edits to a seeded clause survive.
    """
    configure_logging()
    _run(_seed_all())


async def _seed_all() -> None:
    from app.db.seed import seed_all
    from app.db.session import session_scope

    _echo("Seeding reference data...")
    try:
        async with session_scope() as db:
            result = await seed_all(db)
    except Exception as exc:  # noqa: BLE001
        _fail(f"Seeding failed: {exc}")
        return

    for key, value in sorted(result.items()):
        _echo(f"  {key}: {value}")
    _echo("Seed complete.")


# =============================================================================
# Scheduler
# =============================================================================
@app.command()
def scheduler(
    stalled_timeout_minutes: int = typer.Option(
        45, help="Minutes without a heartbeat before a job is treated as stalled."
    ),
    interval_seconds: int = typer.Option(60, help="Sweep interval."),
) -> None:
    """Run the background scheduler.

    Four jobs, each of which exists because something can be left behind:

    * **Stalled-job reclamation.** A crashed worker leaves a job running forever, and
      the contract sits in "processing" with nothing working on it. The heartbeat is
      what distinguishes that from a genuinely slow parse.
    * **Alert evaluation.** Renewal and expiry deadlines are time-based, so something
      has to notice them passing.
    * **Stalled-export recovery.** An export runs as a background task, which dies
      with its process; the row is what survives, and a row stuck in "running" is a
      progress bar the user watches forever.
    * **Expired-export purge.** An export file is a second copy of contract data in
      object storage. Keeping it past its retention window turns every export into a
      permanent copy outside the contract's own lifecycle.
    """
    configure_logging()
    _echo(
        f"Scheduler starting (sweep every {interval_seconds}s, "
        f"stalled after {stalled_timeout_minutes}m)."
    )
    try:
        _run(_scheduler_loop(stalled_timeout_minutes, interval_seconds))
    except KeyboardInterrupt:  # pragma: no cover - operator ctrl-c
        _echo("Scheduler stopped.")


async def _scheduler_loop(stalled_timeout_minutes: int, interval_seconds: int) -> None:
    from app.db.session import session_scope, shutdown_engine

    try:
        while True:
            try:
                async with session_scope() as db:
                    reclaimed = await _reclaim_stalled(db, stalled_timeout_minutes)
                if reclaimed:
                    logger.info("scheduler_reclaimed_jobs", count=reclaimed)
            except Exception as exc:
                logger.exception("scheduler_sweep_failed", error=str(exc))

            # Export sweeps run in their own session and their own try block: a
            # failure here must not stop the pipeline reclamation above from
            # running on the next tick, and vice versa.
            try:
                async with session_scope() as db:
                    from app.export.service import ExportService

                    service = ExportService(db)
                    recovered = await service.recover_stalled()
                    purged = await service.purge_expired()
                if recovered or purged:
                    logger.info("scheduler_export_sweep", recovered=recovered, purged=purged)
            except Exception as exc:
                logger.exception("scheduler_export_sweep_failed", error=str(exc))

            await asyncio.sleep(interval_seconds)
    finally:
        await shutdown_engine()


async def _reclaim_stalled(db: Any, timeout_minutes: int) -> int:
    """Fail jobs whose worker stopped reporting.

    Marked failed rather than requeued: the stage may have been part-way through
    writing rows, and re-running it blindly could duplicate work that the stage's own
    cleanup would otherwise have handled. A failed job is visible and can be
    reprocessed deliberately.
    """
    from app.core.enums import JobState
    from app.repositories.processing import ProcessingJobRepository

    repository = ProcessingJobRepository(db)
    stalled = await repository.find_stalled(timeout_minutes=timeout_minutes)
    for job in stalled:
        logger.warning(
            "job_stalled",
            job_id=str(job.id),
            contract_id=str(job.contract_id),
            state=job.state.value if hasattr(job.state, "value") else str(job.state),
            heartbeat_at=job.heartbeat_at.isoformat() if job.heartbeat_at else None,
        )
        await repository.update(
            job,
            state=JobState.FAILED,
            error_message=(
                f"The worker stopped reporting for more than {timeout_minutes} minutes. "
                "Reprocess the contract to resume."
            ),
        )
    return len(stalled)


# =============================================================================
# Operations
# =============================================================================
@app.command()
def reprocess(
    job: str = typer.Option(..., "--job", help="Job id to reprocess."),
    from_stage: str = typer.Option("validation", "--from-stage", help="Stage to restart from."),
) -> None:
    """Re-run a job from a stage.

    Every stage from ``--from-stage`` onward runs again. Earlier stages keep their
    checkpoints, so re-extracting a contract does not re-parse it.
    """
    configure_logging()
    from app.core.enums import PipelineStage

    try:
        job_id = uuid.UUID(job)
    except ValueError:
        _fail(f"'{job}' is not a valid job id.")
        return

    try:
        stage = PipelineStage(from_stage)
    except ValueError:
        valid = ", ".join(s.value for s in PipelineStage)
        _fail(f"'{from_stage}' is not a pipeline stage. Valid: {valid}")
        return

    _run(_reprocess(job_id, stage))


async def _reprocess(job_id: uuid.UUID, stage: Any) -> None:
    from app.core.enums import stages_from
    from app.db.session import session_scope, shutdown_engine
    from app.orchestrator.queue import StageMessage, get_queue_client
    from app.repositories.processing import ProcessingJobRepository

    try:
        async with session_scope() as db:
            job = await ProcessingJobRepository(db).get(job_id)
            if job is None:
                _fail(f"Job {job_id} does not exist.")
                return
            contract_id = job.contract_id
            project_id = job.project_id

        replay = stages_from(stage)
        _echo(
            f"Reprocessing job {job_id} from '{stage.value}' "
            f"({len(replay)} stage(s): {', '.join(s.value for s in replay)})."
        )

        queue = get_queue_client()
        await queue.enqueue(
            StageMessage(
                job_id=job_id,
                contract_id=contract_id,
                project_id=project_id,
                stage=stage,
                # Force a genuine re-run: without this the runner would reuse the
                # existing checkpoint and the reprocess would be a no-op, which is
                # exactly the opposite of what an operator asking for it wants.
                options={"force": True},
            )
        )
        await queue.close()
        _echo("Queued.")
    finally:
        await shutdown_engine()


@app.command()
def shell() -> None:
    """Open a Python REPL with the session, models and settings preloaded."""
    configure_logging()
    import code

    from app.core.config import get_settings
    from app.db import session as db_session

    banner_settings = get_settings()
    namespace: dict[str, Any] = {
        "settings": banner_settings,
        "session_scope": db_session.session_scope,
        "asyncio": asyncio,
    }

    try:
        import app.models as models

        namespace["models"] = models
    except Exception as exc:  # noqa: BLE001
        _echo(f"(models unavailable: {exc})", err=True)

    code.interact(
        banner=(
            f"CIP shell - env={banner_settings.app_env}\n"
            "Available: settings, models, session_scope, asyncio\n"
            ">>> async with session_scope() as db: ...  "
            "(wrap with asyncio.run)"
        ),
        local=namespace,
    )


@app.command()
def smoke() -> None:
    """Check that every dependency this deployment needs is actually reachable.

    Intended for a post-deploy gate. Reports each dependency separately and exits
    non-zero if any *required* one is down - the AI providers are reported but not
    gating, because the API serves reads perfectly well while a vendor is having an
    outage.
    """
    configure_logging()
    _run(_smoke())


async def _smoke() -> None:
    from app.ai.embedding import embedding_health
    from app.ai.parsers import parser_health
    from app.ai.rag import provider_health
    from app.core.cache import redis_healthy
    from app.core.config import get_settings
    from app.db.session import check_extensions, database_healthy, shutdown_engine
    from app.orchestrator.stages.base import registered_stages, stage_load_errors

    settings = get_settings()
    _echo(f"Environment: {settings.app_env}\n")
    failures: list[str] = []

    try:
        # --- required ---------------------------------------------------------
        db_ok = await database_healthy()
        _echo(f"  database          {'ok' if db_ok else 'FAILED'}")
        if not db_ok:
            failures.append("database")
        else:
            extensions = await check_extensions()
            missing = [name for name, installed in extensions.items() if not installed]
            _echo(f"  extensions        {'ok' if not missing else 'MISSING ' + ', '.join(missing)}")
            if missing:
                # pgvector missing is the silent killer: inserts succeed and every
                # vector search quietly returns nothing.
                failures.append(f"extensions: {', '.join(missing)}")

        redis_ok = await redis_healthy()
        _echo(f"  redis             {'ok' if redis_ok else 'FAILED'}")
        if not redis_ok:
            failures.append("redis")

        stages = registered_stages()
        errors = stage_load_errors()
        _echo(f"  pipeline stages   {len(stages)}/8 registered")
        for name, reason in sorted(errors.items()):
            _echo(f"      {name}: {reason[:80]}")
        if errors:
            failures.append(f"stages unavailable: {', '.join(sorted(errors))}")

        # --- reported, not gating --------------------------------------------
        parsers = await parser_health()
        active = parsers["active"]
        active_ok = parsers["parsers"].get(active, {}).get("healthy", False)
        _echo(f"  parser ({active})   {'ok' if active_ok else 'unreachable'}")
        if not active_ok:
            failures.append(f"parser '{active}' is unreachable")

        llm = await provider_health()
        _echo(
            f"  llm provider      {llm.get('provider')} "
            f"{'ok' if llm.get('healthy') else 'unreachable (not gating)'}"
        )

        embeddings = await embedding_health()
        _echo(
            f"  embeddings        {embeddings.get('provider')} "
            f"{'ok' if embeddings.get('healthy') else 'unreachable (not gating)'}"
        )
    finally:
        await shutdown_engine()

    _echo("")
    if failures:
        _fail("Smoke check failed: " + "; ".join(failures))
    _echo("All required dependencies are reachable.")


@app.command()
def stages() -> None:
    """List the pipeline stages and whether each has a usable handler."""
    configure_logging()
    from app.core.enums import STAGE_ORDER
    from app.orchestrator.stages.base import registered_stages, stage_load_errors

    available = {stage.value for stage in registered_stages()}
    errors = stage_load_errors()

    for index, stage in enumerate(STAGE_ORDER, start=1):
        mark = "ok " if stage.value in available else "-- "
        line = f"  {index}. {mark}{stage.value}"
        reason = errors.get(stage.value)
        if reason:
            line += f"   ({reason[:70]})"
        _echo(line)

    if errors:
        _fail(f"{len(errors)} stage(s) unavailable.")


@app.command("embeddings")
def embeddings_status() -> None:
    """Report the embedding configuration and probe the provider.

    The same diagnostics startup runs, on demand, so an operator can check a change
    before restarting anything.
    """
    configure_logging()
    _run(_embedding_status())


async def _embedding_status() -> None:
    from app.ai.embedding.diagnostics import diagnose
    from app.ai.embedding.reindex import EmbeddingReindexer
    from app.db.session import session_scope

    async with session_scope() as db:
        report = await diagnose(db)
        counts = await EmbeddingReindexer(db).stale_model_counts()

    _echo(f"Provider   : {report.provider}")
    _echo(f"Model      : {report.model}")
    _echo(f"Dimension  : {report.configured_dim} (model native: {report.native_dim})")
    _echo(f"Storage    : {report.storage}")
    if report.reported_dim is not None:
        _echo(f"Reported   : {report.reported_dim} in {report.latency_ms} ms")
    if report.column_type:
        _echo(f"Column     : {report.column_type}({report.column_dim})")
    _echo("")
    for finding in report.findings:
        mark = "ok  " if finding.ok else ("FAIL" if finding.fatal else "warn")
        _echo(f"  [{mark}] {finding.check}: {finding.detail}")

    if counts:
        _echo("\nVectors by model:")
        for model, count in sorted(counts.items()):
            _echo(f"  {model}: {count:,}")

    if not report.ok:
        _fail("The embedding configuration has problems (see above).")


@app.command("reindex-embeddings")
def reindex_embeddings(
    project_id: str = typer.Option("", help="Limit to one project."),
    limit: int = typer.Option(0, help="Stop after N contracts. 0 means all."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Report what would run."),
    yes: bool = typer.Option(False, "--yes", help="Skip the confirmation prompt."),
) -> None:
    """Regenerate vectors for contracts embedded with a different model.

    Safe to interrupt and re-run: the work list is derived from the database, so a
    second run picks up whatever is left rather than starting over. Contracts are
    queued through the normal pipeline from the embedding stage, so progress is
    visible on the Processing screen and retries behave as they do for any job.
    """
    configure_logging()
    _run(_reindex(project_id or None, limit or None, dry_run, yes))


async def _reindex(project_id: str | None, limit: int | None, dry_run: bool, yes: bool) -> None:
    from app.ai.embedding.reindex import EmbeddingReindexer
    from app.core.config import get_settings
    from app.db.session import session_scope

    scope = uuid.UUID(project_id) if project_id else None

    async with session_scope() as db:
        reindexer = EmbeddingReindexer(db)
        counts = await reindexer.stale_model_counts()
        targets = await reindexer.contracts_needing_reindex(project_id=scope, limit=limit)

        if not targets:
            _echo("Every vector already matches the configured model. Nothing to do.")
            return

        _echo(f"Target model : {get_settings().embedding.model}")
        _echo(f"Contracts    : {len(targets)}")
        if counts:
            for model, count in sorted(counts.items()):
                _echo(f"  {model}: {count:,} vector(s)")

        if not dry_run and not yes:
            typer.confirm(
                f"Re-embed {len(targets)} contract(s)? Semantic search for them is "
                "degraded until this completes.",
                abort=True,
            )

        def report(progress: Any) -> None:
            _echo(
                f"  {progress.contracts_done}/{progress.contracts_total} "
                f"({progress.percent}%) queued"
                + (f", {progress.contracts_failed} failed" if progress.contracts_failed else "")
            )

        result = await reindexer.run(
            project_id=scope, limit=limit, on_progress=report, dry_run=dry_run
        )

    if dry_run:
        _echo(f"Dry run: {result.progress.contracts_total} contract(s) would be re-embedded.")
        return

    _echo(
        f"\nQueued {result.progress.contracts_done} contract(s); "
        f"{result.progress.vectors_removed:,} stale vector(s) will be replaced."
    )
    if result.failures:
        for contract_id, error in result.failures[:10]:
            _echo(f"  FAILED {contract_id}: {error}", err=True)
        _fail(f"{len(result.failures)} contract(s) could not be queued.")


@app.command()
def version() -> None:
    """Print component versions, for reproducing an extraction."""
    import json

    from app.core.versions import platform_versions

    for key, value in sorted(platform_versions().items()):
        rendered = json.dumps(value) if isinstance(value, (dict, list)) else value
        _echo(f"  {key}: {rendered}")


# =============================================================================
# Runner
# =============================================================================
def _run(coro: Any) -> None:
    """Run a coroutine, translating a failure into a non-zero exit."""
    try:
        asyncio.run(coro)
    except typer.Exit:
        raise
    except KeyboardInterrupt:  # pragma: no cover
        _echo("Interrupted.", err=True)
        sys.exit(130)
    except Exception as exc:
        logger.exception("cli_command_failed", error=str(exc))
        _fail(str(exc))


def main() -> None:
    app()


if __name__ == "__main__":
    main()
