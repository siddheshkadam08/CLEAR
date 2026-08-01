"""Load testing against a running deployment.

Separate from the benchmark and deliberately so. The benchmark measures *quality*
in-process; this measures what a deployment does under concurrency, over HTTP,
with auth, serialisation, the connection pool and the provider's rate limit all
in play. Those are different questions and conflating them produces a latency
figure that is neither.

It therefore needs a **live target and a real token** - it cannot run in CI
against nothing, and it should not be wired into the quality gate. Its output is
a capacity statement, and the number it exists to find is the concurrency at
which the platform stops keeping up rather than the one at which it is fastest.

Errors are reported, never retried. A retry would hide exactly the saturation the
test is looking for.
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from typing import Any

from app.core.logging import get_logger
from app.evaluation.dataset.loader import load_dataset
from app.evaluation.metrics.performance import percentile

logger = get_logger(__name__)


@dataclass(slots=True)
class LoadTestOptions:
    base_url: str
    token: str
    dataset: str = "smoke"
    levels: list[int] = field(default_factory=lambda: [100, 500, 1000])
    seconds_per_level: int = 60
    #: Seconds between levels, so the pool and the provider's window recover and
    #: the next level measures itself rather than the previous one's backlog.
    cooldown_seconds: int = 15
    endpoint: str = "/api/v1/copilot/query"
    request_timeout: float = 120.0


@dataclass(slots=True)
class LevelResult:
    users: int
    requests: int = 0
    errors: int = 0
    latencies_ms: list[float] = field(default_factory=list)
    status_counts: dict[str, int] = field(default_factory=dict)
    duration_seconds: float = 0.0

    @property
    def throughput_rps(self) -> float:
        return (self.requests / self.duration_seconds) if self.duration_seconds else 0.0

    @property
    def error_rate(self) -> float:
        return (self.errors / self.requests) if self.requests else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "users": self.users,
            "requests": self.requests,
            "errors": self.errors,
            "error_rate": round(self.error_rate, 4),
            "throughput_rps": round(self.throughput_rps, 2),
            "p50_ms": round(percentile(self.latencies_ms, 0.50), 1),
            "p95_ms": round(percentile(self.latencies_ms, 0.95), 1),
            "p99_ms": round(percentile(self.latencies_ms, 0.99), 1),
            "max_ms": round(max(self.latencies_ms, default=0.0), 1),
            "statuses": dict(sorted(self.status_counts.items())),
            "duration_seconds": round(self.duration_seconds, 1),
        }


async def run_load_test(options: LoadTestOptions) -> dict[str, Any]:
    """Step through concurrency levels, recording throughput and latency."""
    import httpx

    dataset = load_dataset(options.dataset)
    questions = [
        (case.question, str(case.project_id) if case.project_id else None) for case in dataset.cases
    ]
    if not questions:
        raise ValueError("the dataset has no questions to send")

    results: list[LevelResult] = []
    url = options.base_url.rstrip("/") + options.endpoint
    headers = {"Authorization": f"Bearer {options.token}"}

    for level in options.levels:
        logger.info("load_level_started", users=level, seconds=options.seconds_per_level)
        result = LevelResult(users=level)
        deadline = time.perf_counter() + options.seconds_per_level
        lock = asyncio.Lock()

        limits = httpx.Limits(max_connections=level + 20, max_keepalive_connections=level)
        async with httpx.AsyncClient(
            timeout=options.request_timeout, limits=limits, headers=headers
        ) as client:

            async def _worker(
                worker_index: int,
                # Bound as defaults rather than captured: the closure outlives one
                # iteration of the level loop, and a late-bound `result` would let
                # a straggler from one level write into the next level's tally.
                level_result: LevelResult = result,
                level_deadline: float = deadline,
                level_lock: asyncio.Lock = lock,
            ) -> None:
                # Seeded per worker so the mix is reproducible between runs while
                # still varying across workers - a load test where every worker
                # sends the same question measures one cache entry. Not a
                # cryptographic use.
                rng = random.Random(worker_index)  # noqa: S311
                while time.perf_counter() < level_deadline:
                    question, project_id = rng.choice(questions)
                    payload: dict[str, Any] = {"query": question}
                    if project_id:
                        payload["projectId"] = project_id

                    started = time.perf_counter()
                    try:
                        response = await client.post(url, json=payload)
                        status = str(response.status_code)
                        failed = response.status_code >= 400
                    except Exception as exc:  # noqa: BLE001 - a failure is a data point
                        status = type(exc).__name__
                        failed = True

                    elapsed = (time.perf_counter() - started) * 1000
                    async with level_lock:
                        level_result.requests += 1
                        level_result.latencies_ms.append(elapsed)
                        level_result.status_counts[status] = (
                            level_result.status_counts.get(status, 0) + 1
                        )
                        if failed:
                            level_result.errors += 1

            started = time.perf_counter()
            await asyncio.gather(*(_worker(index) for index in range(level)))
            result.duration_seconds = time.perf_counter() - started

        results.append(result)
        logger.info("load_level_complete", **result.as_dict())

        if level != options.levels[-1]:
            await asyncio.sleep(options.cooldown_seconds)

    return {
        "target": options.base_url,
        "dataset": dataset.identifier,
        "seconds_per_level": options.seconds_per_level,
        "levels": [result.as_dict() for result in results],
        "verdict": _verdict(results),
    }


def _verdict(results: list[LevelResult]) -> str:
    """Where the platform stopped keeping up, and what gave way.

    Saturation is called on the *first* level that breaches either bound, not the
    worst: past the knee every subsequent number describes a queue rather than the
    system, and reporting the worst level would overstate capacity by implying the
    ones before it were fine.
    """
    if not results:
        return "No levels completed."

    for result in results:
        if result.error_rate > 0.01:
            return (
                f"Saturates at {result.users} concurrent users: "
                f"{result.error_rate:.1%} of requests failed "
                f"({result.throughput_rps:.1f} rps, p95 {percentile(result.latencies_ms, 0.95):.0f} ms). "
                "Check the connection pool and the provider's rate limit before "
                "reading anything above this level."
            )
        p95 = percentile(result.latencies_ms, 0.95)
        if p95 > 30_000:
            return (
                f"Degrades at {result.users} concurrent users: p95 {p95:.0f} ms with no "
                "errors yet. Requests are queueing rather than failing, which is the "
                "shape of pool exhaustion."
            )

    best = results[-1]
    return (
        f"Sustained {best.users} concurrent users at {best.throughput_rps:.1f} rps, "
        f"p95 {percentile(best.latencies_ms, 0.95):.0f} ms, "
        f"{best.error_rate:.2%} errors. No saturation point found - test higher."
    )


__all__ = ["LevelResult", "LoadTestOptions", "run_load_test"]
