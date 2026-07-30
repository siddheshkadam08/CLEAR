# CIP queue — logic-free dispatch layer

BullMQ is used **only** to move messages. Every decision about a contract is made in
Python; this service knows nothing about contracts, clauses or pipelines beyond a
list of queue names.

That constraint is deliberate and load-bearing (§1.2). It is what makes the queue
swappable, and it is why there is no second implementation of the pipeline's rules
to drift out of sync with the first.

## What this service does

```
  Python                     Node (this service)              Python
  ──────                     ───────────────────              ──────
  enqueue()  ──POST /enqueue──►  BullMQ queue
                                      │
                                      ▼
                                 worker takes it
                                      │
                                      └──POST /internal/stages/{stage}/run──►  run_stage()
                                                                                   │
                                      ◄──── { status, should_retry, … } ───────────┘
                                      │
                        honours should_retry / dead-letters
```

Three responsibilities, and nothing else:

1. **Accept** a stage message on `POST /enqueue` and put it on that stage's queue.
2. **Dispatch** it to the Python stage endpoint and wait for the reply.
3. **Translate** the reply into a BullMQ outcome — complete, retry, or dead-letter.

## What this service explicitly does not do

- Decide which stage runs next. Python's runner dispatches the next stage, so
  pipeline sequencing lives in one place.
- Decide whether a failure is retryable. Python classifies the error and returns
  `should_retry` and `retry_delay_ms`.
- Inspect, parse or store document content. Messages carry identifiers only, which
  keeps them small, safe to log, and means a re-delivered message reads current
  state rather than acting on a stale snapshot.
- Touch the database. It has no credentials for one.

## The one judgement it does make

A **stage failure** and a **transport failure** are not the same thing:

| | What happened | Who retries |
|---|---|---|
| `200` + `should_retry: true` | The stage ran and failed. Python recorded it and asked for a retry. | BullMQ, at Python's delay |
| `200` + `should_retry: false` | The stage ran and failed permanently, or succeeded. | Nobody |
| `5xx` / connection error | The call never got through. **Nothing was recorded.** | BullMQ, on its own backoff |
| `4xx` | The message is malformed and will be on every attempt. | Nobody — straight to the DLQ |

Conflating the first and third would either double-run stages or silently drop
them, which is why the distinction is explicit in `worker.ts`.

## Dead-letter queue

A job that exhausts its attempts moves to `<prefix>:dlq`. It is *not* left in
BullMQ's own failed list, because retention trims that on a timer and a job needing
a human decision must not disappear on one.

```
GET    /dlq                 list entries
GET    /dlq/size            depth, for the admin screen
POST   /dlq/:id/replay      requeue at attempt 1, after fixing the cause
DELETE /dlq/:id             discard
```

Replay resets the attempt counter deliberately: the retry budget belongs to a
dispatch decision, and an operator replaying a job after fixing the cause is making
a new one.

## Endpoints

| Route | Auth | Purpose |
|---|---|---|
| `GET /healthz` | none | container liveness probe |
| `GET /metrics` | none | Prometheus scrape |
| `POST /enqueue` | token | queue a stage message |
| `GET /queues` | token | per-queue depth |
| `GET /dlq`, `POST /dlq/:id/replay`, `DELETE /dlq/:id` | token | dead-letter administration |

`X-Internal-Token` must match the backend's `INTERNAL_API_TOKEN`. The service is not
publicly exposed, but a dispatcher that would queue work for anyone who can reach it
is one network misconfiguration away from a problem.

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `REDIS_URL` | `redis://localhost:6379/0` | shared with the backend |
| `QUEUE_PREFIX` | `cip` | must match the backend's `QUEUE_PREFIX` |
| `INTERNAL_API_BASE_URL` | `http://localhost:8000` | where the stage endpoints live |
| `INTERNAL_API_TOKEN` | *(required)* | shared secret |
| `QUEUE_STAGE_TIMEOUT_MS` | `1800000` | a 300-page scanned parse legitimately takes minutes |
| `QUEUE_MAX_ATTEMPTS` | `3` | transport-level backstop only |
| `QUEUE_BACKOFF_MS` | `5000` | exponential base |
| `WORKER_CONCURRENCY_<STAGE>` | per stage | e.g. `WORKER_CONCURRENCY_PARSER=20` |

## Replacing BullMQ with arq or Celery

The worker→service contract is the entire interface, so a pure-Python dispatcher is
a drop-in replacement. Set `QUEUE_DRIVER=arq` on the backend and run something
equivalent to:

```python
# arq worker - the complete equivalent of worker.ts
import httpx
from arq import cron  # noqa: F401  (scheduled sweeps, if wanted)

async def run_stage(ctx, message: dict) -> dict:
    """Take a message, POST it to Python, honour the reply. No logic here either."""
    stage = message["stage"]
    attempt = ctx["job_try"]

    async with httpx.AsyncClient(timeout=1800.0) as client:
        response = await client.post(
            f"{BASE_URL}/internal/stages/{stage}/run",
            json={**message, "attempt": attempt},
            headers={"X-Internal-Token": TOKEN},
        )

    # Transport failure: nothing was recorded, so retrying is safe and necessary.
    if response.status_code >= 500:
        raise ConnectionError(f"stage endpoint {response.status_code}")
    # 4xx: the message is malformed and will be on every attempt.
    response.raise_for_status()

    result = response.json()
    if result["should_retry"]:
        # arq's Retry carries the delay Python chose.
        from arq.worker import Retry
        raise Retry(defer=result["retry_delay_ms"] / 1000)
    return result


class WorkerSettings:
    functions = [run_stage]
    max_tries = 3
    # One arq queue per stage, matching QUEUE_PREFIX:<stage>.
    queue_name = "cip:parser"
```

Celery is the same shape: an `autoretry_for` on the transport exception, and an
explicit `self.retry(countdown=result["retry_delay_ms"] / 1000)` when
`should_retry` is set.

What must hold in any implementation:

1. Queue names are `<QUEUE_PREFIX>:<stage>` — the backend's `IQueueClient.queue_name`
   builds the same string.
2. `should_retry` and `retry_delay_ms` are honoured as given, not recomputed.
3. A transport failure retries; a `4xx` does not.
4. Exhausted jobs land somewhere durable and listable.
5. The next stage is never enqueued by the dispatcher.

Meet those and nothing in Python changes.

## Development

```bash
npm install
npm run dev          # tsx watch
npm run typecheck    # tsc --noEmit
npm run lint         # eslint, zero warnings
npm run build        # dist/
```
