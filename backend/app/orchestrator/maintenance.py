"""Periodic maintenance sweeps.

Four jobs, each of which exists because something can be left behind:

* **Stalled-job reclamation.** A crashed worker leaves a job running forever and the
  contract sits in "processing" with nothing working on it. The heartbeat is what
  distinguishes that from a genuinely slow parse.
* **Stage-queue lease reclamation.** Only meaningful under ``QUEUE_DRIVER=postgres``,
  where a worker killed mid-stage leaves its row ``claimed`` for ever. BullMQ does
  its own stalled-job recovery inside the Worker (it has since v4, which is why
  there is no separate ``QueueScheduler``), so this sweep is skipped for it rather
  than made conditional inside the driver.
* **Export recovery and purge.** An export runs as a background task that dies with
  its process; the row is what survives, and one stuck in "running" is a progress bar
  the user watches for ever. An expired export file is a second copy of contract data
  outside the contract's own lifecycle.
* **Alert evaluation.** Expiries, renewal notice windows, obligation deadlines, risk
  scores and review backlogs - none of which anything pushes, so this is the only
  thing that notices them. Runs on its own much slower cadence
  (``ALERT_EVALUATOR_INTERVAL_MINUTES``): reclamation has to be prompt because a
  stalled job blocks a user, while alert conditions move by the calendar and
  re-deriving every contract's deadlines once a minute would be a full-table scan per
  minute to reach the same answer.

**None of this is BullMQ's ``QueueScheduler``**, which the queue has never used and
which v5 does not have. That component promoted delayed jobs and recovered stalled
ones *within* the broker. These sweeps are about the application's own state: a
contract expiring next Tuesday is not something a queue can have an opinion about.

These ran in a dedicated ``scheduler`` service. They now run inside each worker
process, which is what removes that container from a deployment - see
:func:`run_maintenance_sweep` on why that is safe with several workers.
``app.cli scheduler`` still runs them standalone for deployments that prefer to keep
them apart.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)

#: Advisory-lock key for the whole sweep. Arbitrary but fixed - the only requirement
#: is that no other component in this database picks the same number. Postgres
#: advisory locks share one namespace per database.
MAINTENANCE_LOCK_KEY = 8_474_213_001


async def run_maintenance_sweep(
    *,
    stalled_timeout_minutes: int = 45,
    include_alerts: bool = True,
) -> dict[str, int]:
    """Run one pass of every sweep. Returns what each one changed.

    **Only one caller anywhere runs the sweeps per tick**, enforced by a Postgres
    advisory lock. Without it every worker would run every sweep: N processes failing
    the same stalled jobs, and N ``AlertEvaluator`` runs reconciling the same alert
    set against each other. A worker that does not get the lock returns immediately
    and tries again on its next tick, which is why the loser is not made to wait.

    Each sweep gets its own session and its own ``try`` block. A failure in one must
    not stop the others running on this tick, and none of them must ever take the
    process down - a sweep that raised into the worker's event loop would stop the
    worker serving stages, which is far worse than the sweep being skipped.
    """
    from app.core.config import get_settings
    from app.db.session import session_scope

    changed = {"jobs": 0, "leases": 0, "exports_recovered": 0, "exports_purged": 0, "alerts": 0}

    async with _sweep_lock() as acquired:
        if not acquired:
            logger.debug("maintenance_sweep_skipped", reason="lock_held_elsewhere")
            return changed

        try:
            async with session_scope() as db:
                changed["jobs"] = await reclaim_stalled_jobs(db, stalled_timeout_minutes)
            if changed["jobs"]:
                logger.info("maintenance_reclaimed_jobs", count=changed["jobs"])
        except Exception as exc:
            logger.exception("maintenance_job_sweep_failed", error=str(exc))

        # The job-level reclamation above notices a contract whose worker went quiet;
        # this notices the queue *row* it was holding. Postgres driver only - see the
        # module docstring on why BullMQ needs nothing here.
        try:
            if get_settings().queue.driver == "postgres":
                from app.orchestrator.queue import PostgresQueueDriver

                changed["leases"] = await PostgresQueueDriver().reclaim_stale(
                    lease_seconds=stalled_timeout_minutes * 60
                )
                if changed["leases"]:
                    logger.info("maintenance_reclaimed_leases", count=changed["leases"])
        except Exception as exc:
            logger.exception("maintenance_lease_sweep_failed", error=str(exc))

        try:
            async with session_scope() as db:
                from app.export.service import ExportService

                service = ExportService(db)
                changed["exports_recovered"] = await service.recover_stalled()
                changed["exports_purged"] = await service.purge_expired()
            if changed["exports_recovered"] or changed["exports_purged"]:
                logger.info(
                    "maintenance_export_sweep",
                    recovered=changed["exports_recovered"],
                    purged=changed["exports_purged"],
                )
        except Exception as exc:
            logger.exception("maintenance_export_sweep_failed", error=str(exc))

        if include_alerts:
            try:
                async with session_scope() as db:
                    from app.services.alert_evaluator import AlertEvaluator

                    outcome = await AlertEvaluator(db).run()
                if outcome.changed():
                    changed["alerts"] = 1
                    logger.info("maintenance_alert_sweep", **outcome.as_log_fields())
            except Exception as exc:
                logger.exception("maintenance_alert_sweep_failed", error=str(exc))

    return changed


async def maintenance_loop(
    *,
    stalled_timeout_minutes: int = 45,
    interval_seconds: int = 60,
) -> None:
    """Sweep forever. Each worker runs this as a background task; so does the CLI.

    Cancellation-safe: ``asyncio.sleep`` is the only await outside the sweep, so a
    cancelled task stops between ticks rather than mid-write.
    """
    from app.core.config import get_settings

    alert_interval = get_settings().alerts.evaluator_interval_minutes * 60
    # Due immediately on the first tick, so a freshly started process populates the
    # Alerts screen rather than leaving it empty for an hour.
    next_alert_sweep = 0.0

    while True:
        now = asyncio.get_running_loop().time()
        due = now >= next_alert_sweep
        if due:
            next_alert_sweep = now + alert_interval

        await run_maintenance_sweep(
            stalled_timeout_minutes=stalled_timeout_minutes,
            include_alerts=due,
        )
        await asyncio.sleep(interval_seconds)


async def reclaim_stalled_jobs(db: Any, timeout_minutes: int) -> int:
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


class _sweep_lock:  # noqa: N801 - used as a context manager, reads as one
    """Hold ``pg_try_advisory_lock`` on one dedicated connection for the whole sweep.

    A session-scoped advisory lock belongs to the *connection* that took it. Taking
    it through ``session_scope`` would return that connection to the pool while the
    lock was still held, so the unlock could land on a different connection and fail
    silently - leaving the lock held until the process exits and every later sweep
    skipped. Hence an explicit connection, held open across the sweep and closed in
    ``finally``.

    Yields whether the lock was acquired. It never raises for contention: losing the
    race is the expected outcome for every worker but one.
    """

    def __init__(self, key: int = MAINTENANCE_LOCK_KEY) -> None:
        self._key = key
        self._conn: Any = None
        self._acquired = False

    async def __aenter__(self) -> bool:
        from sqlalchemy import text

        from app.db.session import get_engine

        try:
            self._conn = await get_engine().connect()
            result = await self._conn.execute(
                text("SELECT pg_try_advisory_lock(:key)"), {"key": self._key}
            )
            self._acquired = bool(result.scalar())
        except Exception as exc:  # noqa: BLE001 - the database being down is the
            # sweep's problem to survive, not to crash on. Reported and retried on
            # the next tick.
            logger.warning("maintenance_lock_unavailable", error=str(exc)[:200])
            await self._release_connection()
            return False

        if not self._acquired:
            await self._release_connection()
        return self._acquired

    async def __aexit__(self, *exc_info: Any) -> None:
        if self._acquired and self._conn is not None:
            from sqlalchemy import text

            try:
                await self._conn.execute(
                    text("SELECT pg_advisory_unlock(:key)"), {"key": self._key}
                )
            except Exception as exc:  # noqa: BLE001
                # The lock dies with the connection anyway, so a failed unlock is
                # noise rather than a leak.
                logger.debug("maintenance_unlock_failed", error=str(exc)[:200])
        await self._release_connection()

    async def _release_connection(self) -> None:
        if self._conn is not None:
            try:
                await self._conn.close()
            except Exception as exc:  # noqa: BLE001
                logger.debug("maintenance_lock_close_failed", error=str(exc)[:200])
            self._conn = None


__all__ = [
    "MAINTENANCE_LOCK_KEY",
    "maintenance_loop",
    "reclaim_stalled_jobs",
    "run_maintenance_sweep",
]
