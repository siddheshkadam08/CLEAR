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
import os
import socket
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
    """Run the maintenance sweeps as a standalone process.

    **Not required in a normal deployment.** Every worker runs the same sweeps itself
    (see ``app.orchestrator.maintenance``), which is what removes the ``scheduler``
    service from compose. This command exists for deployments that would rather run
    them apart from the pipeline, and for running a sweep by hand.

    Running both is safe: the sweeps take a Postgres advisory lock, so only one
    process anywhere performs them per tick. Set ``WORKER_MAINTENANCE=false`` on the
    workers if the sweeps are split out this way, so they stop trying for a lock they
    will never need.

    Not to be confused with BullMQ's ``QueueScheduler``, which this queue has never
    used and which BullMQ v5 does not have - that promoted delayed jobs inside the
    broker, whereas these sweeps are about the application's own state.
    """
    configure_logging()
    _echo(
        f"Scheduler starting (sweep every {interval_seconds}s, "
        f"stalled after {stalled_timeout_minutes}m)."
    )
    try:
        _run(_standalone_scheduler(stalled_timeout_minutes, interval_seconds))
    except KeyboardInterrupt:  # pragma: no cover - operator ctrl-c
        _echo("Scheduler stopped.")


async def _standalone_scheduler(stalled_timeout_minutes: int, interval_seconds: int) -> None:
    from app.db.session import shutdown_engine
    from app.orchestrator.maintenance import maintenance_loop

    try:
        await maintenance_loop(
            stalled_timeout_minutes=stalled_timeout_minutes,
            interval_seconds=interval_seconds,
        )
    finally:
        await shutdown_engine()


@app.command(name="evaluate-alerts")
def evaluate_alerts(
    project_id: str = typer.Option(
        "", help="Limit the sweep to one project. Omit to evaluate every project."
    ),
    as_of: str = typer.Option(
        "", help="Evaluate as though today were this date (YYYY-MM-DD). For demos and tests."
    ),
) -> None:
    """Run one alert-evaluation pass now.

    The same sweep the scheduler runs hourly, on demand. Useful after importing
    contracts (the scheduler would otherwise take up to an hour to notice them),
    after retuning a rule, and for checking what a threshold change would do
    before leaving it in place.

    Writes are committed only if the whole pass succeeds.
    """
    configure_logging()

    scope: uuid.UUID | None = None
    if project_id:
        try:
            scope = uuid.UUID(project_id)
        except ValueError:
            _fail(f"'{project_id}' is not a valid project id.")

    today = None
    if as_of:
        from datetime import date as _date

        try:
            today = _date.fromisoformat(as_of)
        except ValueError:
            _fail(f"'{as_of}' is not a valid date. Use YYYY-MM-DD.")

    # Reports from inside the coroutine rather than returning the outcome: `_run`
    # discards its result, and it is the thing that turns a failure into a
    # non-zero exit, which is what a scripted run depends on.
    async def _run_once() -> None:
        from app.db.session import session_scope, shutdown_engine
        from app.services.alert_evaluator import AlertEvaluator

        try:
            async with session_scope() as db:
                outcome = await AlertEvaluator(db, today=today).run(project_id=scope)
        finally:
            await shutdown_engine()

        _echo(
            f"Examined {outcome.contracts_examined} contracts and "
            f"{outcome.obligations_examined} obligations against {outcome.rules_applied} rules."
        )
        _echo(
            f"Raised {outcome.raised}, refreshed {outcome.refreshed}, "
            f"retired {outcome.retired}, escalated {outcome.escalated}."
        )

    _run(_run_once())


# =============================================================================
# Worker
# =============================================================================
@app.command()
def worker(
    poll_seconds: float = typer.Option(
        1.0, help="Idle wait between polls when there was nothing to claim."
    ),
    lease_seconds: int = typer.Option(
        1800, help="How long a claim is honoured before the scheduler reclaims it."
    ),
    name: str = typer.Option("", help="Worker id recorded on claimed rows. Defaults to host:pid."),
) -> None:
    """Run stages from the Postgres queue.

    The counterpart to ``QUEUE_DRIVER=postgres`` - it replaces the Node BullMQ
    dispatcher *and* its workers, so neither Redis nor the queue service is
    needed. Run as many of these as you like; ``FOR UPDATE SKIP LOCKED`` is what
    lets them share one table without coordinating.

    Stop with ctrl-c. In-flight stages are allowed to finish; their rows are only
    marked done once they actually are, so a stage interrupted harder than that
    is recovered by the lease timeout rather than lost.
    """
    from app.core.config import get_settings

    settings = get_settings()
    if settings.queue.driver != "postgres":
        _fail(
            f"QUEUE_DRIVER is '{settings.queue.driver}'. This worker only serves the "
            "Postgres queue; set QUEUE_DRIVER=postgres."
        )

    configure_logging()
    worker_id = name or f"{socket.gethostname()}:{os.getpid()}"
    _echo(f"Worker {worker_id} starting (poll {poll_seconds}s, lease {lease_seconds}s).")
    try:
        _run(_worker_loop(worker_id, poll_seconds, lease_seconds))
    except KeyboardInterrupt:  # pragma: no cover - operator ctrl-c
        _echo("Worker stopped.")


async def _worker_loop(worker_id: str, poll_seconds: float, lease_seconds: int) -> None:
    from app.core.config import get_settings
    from app.core.enums import STAGE_ORDER, PipelineStage
    from app.db.session import shutdown_engine
    from app.orchestrator.queue import PostgresQueueDriver

    settings = get_settings()
    driver = PostgresQueueDriver()

    # Claimed per stage, so a stage cannot exceed its own concurrency even while
    # the others are idle.
    running: dict[PipelineStage, set[asyncio.Task[None]]] = {stage: set() for stage in STAGE_ORDER}

    # The maintenance sweeps, same as `worker_app` starts for the BullMQ pools. Both
    # entry points run them because either can be the only worker a deployment has -
    # this one is what `docker-compose.vm.yml` runs. The advisory lock inside makes
    # running several of them, of either kind, safe.
    #
    # Beside the claim loop, not inside it: a sweep must not delay claiming, and a
    # claim loop busy with eight stages must not delay a sweep.
    maintenance: asyncio.Task[None] | None = None
    if settings.worker_maintenance:
        from app.orchestrator.maintenance import maintenance_loop

        maintenance = asyncio.create_task(maintenance_loop())

    try:
        while True:
            claimed_any = False

            # One claim per stage rather than one claim overall. A single query
            # with a combined limit could return a batch that is entirely
            # `docpipeline`, blowing past that stage's cap of 4 while parser sits
            # idle. These are indexed lookups against a partial index, so the
            # extra round trips cost far less than the mistake would.
            for stage in STAGE_ORDER:
                free = settings.queue.concurrency_for(stage.value) - len(running[stage])
                if free <= 0:
                    continue

                for item in await driver.claim(worker_id=worker_id, limit=free, stages=[stage]):
                    task = asyncio.create_task(_run_claimed(driver, item))
                    running[stage].add(task)
                    task.add_done_callback(running[stage].discard)
                    claimed_any = True

            if not claimed_any:
                await asyncio.sleep(poll_seconds)
    finally:
        # Cancelled first, and awaited: the sweep holds an advisory lock on its own
        # connection, and tearing the engine down underneath it would leak both.
        if maintenance is not None:
            maintenance.cancel()
            await asyncio.gather(maintenance, return_exceptions=True)

        in_flight = [task for tasks in running.values() for task in tasks]
        if in_flight:
            _echo(f"Finishing {len(in_flight)} in-flight stage(s)...")
            await asyncio.gather(*in_flight, return_exceptions=True)
        await shutdown_engine()


async def _run_claimed(driver: Any, item: Any) -> None:
    """Run one claimed row, then settle its lease.

    The division of labour with ``run_stage`` matters and is easy to get wrong.
    ``run_stage`` never raises for a *stage* failure: it records the error and,
    when the stage is retryable, enqueues a **fresh** message with ``attempt+1``
    and a backoff. That new message is a new queue row.

    So a returned outcome - success or failure - means this row is finished, and
    it is marked ``done``. Failing it here as well would schedule a second retry
    for the same failure and double the pipeline's attempt budget.

    ``fail`` is therefore reserved for ``run_stage`` *raising*, which means
    something outside the stage broke (the database went away mid-run, the
    process ran out of memory). That is worth retrying at the queue level,
    because nothing else recorded it.
    """
    from app.orchestrator.runner import run_stage

    try:
        outcome = await run_stage(item.message)
    except Exception as exc:
        logger.exception(
            "worker_stage_crashed",
            stage=item.message.stage.value,
            job_id=str(item.message.job_id),
            error=str(exc),
        )
        await driver.fail(
            item.row_id,
            error={"message": str(exc), "type": type(exc).__name__, "attempt": item.attempt},
        )
        return

    await driver.complete(item.row_id)
    logger.info(
        "worker_stage_settled",
        stage=item.message.stage.value,
        job_id=str(item.message.job_id),
        status=outcome.status.value,
    )


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
        reindexer = EmbeddingReindexer(db)
        counts = await reindexer.stale_model_counts()
        audit = await reindexer.audit()

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

    # The check that matters more than the counts: are the stored vectors all in
    # one space? A mixed store answers queries without erroring, so nothing else
    # in the system will report this.
    if not audit.is_consistent:
        _echo(f"\n  [FAIL] {audit.incompatible_rows:,} vector(s) are not in {audit.active.label}:")
        for label in audit.foreign_spaces:
            _echo(f"    {label}: {audit.spaces[label]:,}")
        _echo("    Similarity search across these is meaningless. Run:")
        _echo("      cip reindex-embeddings")

    if not report.ok:
        _fail("The embedding configuration has problems (see above).")


@app.command("reindex-embeddings")
def reindex_embeddings(
    project_id: str = typer.Option("", help="Limit to one project."),
    limit: int = typer.Option(0, help="Stop after N contracts. 0 means all."),
    all_contracts: bool = typer.Option(
        False,
        "--all",
        help="Re-embed every contract, including ones already in the target space.",
    ),
    batch_size: int = typer.Option(25, help="Contracts between progress log lines."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Report what would run."),
    yes: bool = typer.Option(False, "--yes", help="Skip the confirmation prompt."),
) -> None:
    """Regenerate vectors that are not in the configured embedding space.

    "Not in the space" means any of provider, model, dimension, embedding version
    or strategy version differs - not just the model name. Vectors from two spaces
    cannot be compared, so a partially-migrated index answers queries with a
    ranking that has no meaning.

    Safe to interrupt and re-run: the work list is derived from the database, so a
    second run picks up whatever is left rather than starting over. Contracts are
    queued through the normal pipeline from the embedding stage, so progress is
    visible on the Processing screen and retries behave as they do for any job.

    ``--all`` forces every contract, for when the vectors are suspect for a reason
    the provenance columns cannot express.
    """
    configure_logging()
    _run(
        _reindex(
            project_id or None,
            limit or None,
            dry_run,
            yes,
            include_current=all_contracts,
            batch_size=batch_size,
        )
    )


async def _reindex(
    project_id: str | None,
    limit: int | None,
    dry_run: bool,
    yes: bool,
    *,
    include_current: bool = False,
    batch_size: int = 25,
) -> None:
    from app.ai.embedding.reindex import EmbeddingReindexer
    from app.db.session import session_scope

    scope = uuid.UUID(project_id) if project_id else None

    async with session_scope() as db:
        reindexer = EmbeddingReindexer(db)
        audit = await reindexer.audit(scope)
        targets = await reindexer.contracts_needing_reindex(
            project_id=scope, limit=limit, include_current=include_current
        )

        if not targets:
            _echo(f"Every vector is already in {audit.active.label}. Nothing to do.")
            return

        _echo(f"Target space : {audit.active.label}")
        _echo(f"Contracts    : {len(targets)}")
        if audit.spaces:
            _echo("Stored vectors by space:")
            for label, count in sorted(audit.spaces.items()):
                mark = "  " if label == audit.active.label else "! "
                _echo(f"  {mark}{label}: {count:,} vector(s)")
        if not audit.is_consistent:
            _echo(
                f"\n  {audit.incompatible_rows:,} vector(s) are in a space that cannot be "
                "compared with the active one. Search results involving them are "
                "not meaningful until this completes."
            )

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
            project_id=scope,
            limit=limit,
            on_progress=report,
            dry_run=dry_run,
            include_current=include_current,
            batch_size=batch_size,
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


@app.command("process-doc")
def process_doc(
    json_dir: str = typer.Argument(..., help="Directory of page_*.json from the PDF service."),
    pdf_path: str = typer.Option("", help="Path of the source PDF, recorded on the master row."),
    pages: int = typer.Option(5, help="Pages the classifier reads."),
    chunk_pages: int = typer.Option(4, help="Pages per clause-search call."),
    chunk_overlap: int = typer.Option(0, help="Pages re-read at the start of each window."),
    concurrency: int = typer.Option(4, help="Clause-search calls to run at once."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Extract and print only. No LLM, no DB."),
    no_persist: bool = typer.Option(
        False, "--no-persist", help="Classify and detect, write nothing."
    ),
    no_early_stop: bool = typer.Option(
        False, "--no-early-stop", help="Search every chunk even after all clauses are found."
    ),
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Print every page, and the full text of every clause found."
    ),
) -> None:
    """Classify a document, find its clauses, embed them and record the result.

    Reads the per-page JSON already on disk - the PDF service is never called.
    The document type decides which clauses to look for, via ``cip_docMapping``;
    each clause found is written to ``cip_DocContentMaster`` with its page
    numbers, bounding box and embedding.
    """
    _use_utf8_stdout()
    configure_logging()

    target = Path(json_dir).expanduser()
    if not target.is_dir():
        _fail(f"Not a directory: {target}")

    _run(
        _process_doc(
            target,
            pdf_path or None,
            pages,
            chunk_pages,
            chunk_overlap,
            concurrency,
            classify=not dry_run,
            persist=not (dry_run or no_persist),
            early_stop=not no_early_stop,
            verbose=verbose,
        )
    )


async def _process_doc(
    json_dir: Path,
    pdf_path: str | None,
    page_window: int,
    chunk_pages: int,
    chunk_overlap: int,
    concurrency: int,
    *,
    classify: bool,
    persist: bool,
    early_stop: bool,
    verbose: bool,
) -> None:
    from app.ai.docpipeline import run_document_pipeline

    try:
        await run_document_pipeline(
            json_dir,
            pdf_path=pdf_path,
            page_window=page_window,
            chunk_pages=chunk_pages,
            chunk_overlap=chunk_overlap,
            concurrency=concurrency,
            early_stop=early_stop,
            classify=classify,
            persist=persist,
            verbose=verbose,
            emit=_echo,
        )
    except (FileNotFoundError, LookupError) as exc:
        _fail(str(exc))


@app.command("fix-embedding-dimension")
def fix_embedding_dimension(
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the SQL without executing it."),
) -> None:
    """Move `embeddings.embedding` to the configured model's width.

    **Destructive**: existing vectors are deleted, not converted. Vectors from two
    different models do not share a space, so re-running the pipeline is the only
    way to repopulate them. See ``sql/embedding_dimension.sql``.

    Replaces ``fix-cip-schema``, which also patched three externally-owned
    ``cip_*`` tables the platform no longer reads or writes.
    """
    configure_logging()
    script = _BACKEND_ROOT / "sql" / "embedding_dimension.sql"
    if not script.is_file():
        _fail(f"Missing {script}")

    sql = script.read_text(encoding="utf-8")
    if dry_run:
        _echo(sql)
        return

    statements = _split_sql(sql)
    _run(_apply_sql(statements))
    _echo(f"Embedding dimension applied ({len(statements)} statements).")


def _split_sql(sql: str) -> list[str]:
    """Split a script into individual statements.

    asyncpg sends each statement as a prepared statement and refuses more than
    one per call, so the script cannot be handed over whole. Splitting naively on
    ``;`` would cut the ``DO $$ ... $$`` block in half, so dollar-quoted bodies
    are tracked and their semicolons ignored.
    """
    statements: list[str] = []
    current: list[str] = []
    in_dollar = False

    for line in sql.splitlines():
        stripped = line.strip()
        if not in_dollar and (not stripped or stripped.startswith("--")):
            continue

        if line.count("$$") % 2 == 1:
            in_dollar = not in_dollar

        current.append(line)
        if not in_dollar and stripped.endswith(";"):
            statements.append("\n".join(current).strip())
            current = []

    if current:
        statements.append("\n".join(current).strip())
    return [statement for statement in statements if statement]


async def _apply_sql(statements: list[str]) -> None:
    from app.db.session import session_scope

    async with session_scope() as db:
        connection = await db.connection()
        for statement in statements:
            # exec_driver_sql, not text(): `$$` and `%` in the DDL would
            # otherwise be read as bind-parameter syntax.
            await connection.exec_driver_sql(statement)


def _use_utf8_stdout() -> None:
    """Make stdout able to carry the document's own script.

    Contract pages carry Devanagari, accented Latin and typographic quotes. A
    default Windows console is cp1252, so the first such paragraph aborts the
    command with UnicodeEncodeError - the pipeline works and the report dies.
    """
    stream = sys.stdout
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]


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
