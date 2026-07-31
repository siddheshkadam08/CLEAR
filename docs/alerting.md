# Operational alerting

When ingestion fails, two different people need to know, and they need different
things. A **reviewer** needs the failure to appear in the Alerts screen next to
the document. An **operator** needs a stack trace, a worker name and a host,
delivered somewhere they are already watching.

`AlertService` does both from one call. Every alert is persisted as an `alerts`
row *and* dispatched to the configured notification channels.

> **Why this exists.** `app/orchestrator/runner.py` imported
> `app.services.alerts` inside a `try/except Exception` block. The module did not
> exist, so every terminal ingestion failure hit `ImportError`, was swallowed by
> the catch-all, and logged one `failure_alert_not_raised` line. Failures were
> recorded on the job row and nowhere else — no alert, no notification, nothing
> that would reach a person.

---

## Quick start

Nothing to configure. The default provider is `console`, which writes a
structured log record and needs no credentials, no network and no vendor account:

```bash
ALERT_PROVIDER=console
ALERT_MIN_LEVEL=ERROR
```

To send failures to Slack instead:

```bash
ALERT_PROVIDER=slack
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/T000/B000/xxxx
```

To fan out to several channels, use a CSV — this is a config change, not a code
change:

```bash
ALERT_PROVIDER=console,slack,webhook
```

---

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `ALERT_ENABLED` | `true` | Master switch for **delivery**. Alerts are still persisted and logged when off. |
| `ALERT_PROVIDER` | `console` | CSV of `console`, `slack`, `teams`, `webhook`, `email`, `null`. |
| `ALERT_MIN_LEVEL` | `ERROR` | `INFO`, `WARNING`, `ERROR`, `CRITICAL`. Below this, nothing is delivered. |
| `ALERT_TIMEOUT_SECONDS` | `10` | Per-attempt network timeout. |
| `ALERT_RETRY_ATTEMPTS` | `3` | Attempts per provider, per alert. |
| `ALERT_RETRY_BACKOFF_SECONDS` | `0.5` | First backoff delay; doubles per attempt. |
| `ALERT_RETRY_BACKOFF_MAX_SECONDS` | `8` | Backoff ceiling. |
| `SLACK_WEBHOOK_URL` | — | Slack incoming webhook. |
| `TEAMS_WEBHOOK_URL` | — | Teams incoming webhook. |
| `ALERT_WEBHOOK_URL` | — | Generic JSON receiver (PagerDuty, Opsgenie, internal bus). |
| `ALERT_WEBHOOK_TOKEN` | — | Optional bearer token for the generic webhook. |
| `SMTP_HOST` / `SMTP_PORT` | — / `587` | SMTP relay. |
| `SMTP_USERNAME` / `SMTP_PASSWORD` | — | Optional; anonymous relay is supported. |
| `SMTP_FROM` | — | Envelope sender. |
| `SMTP_TO` | — | CSV of recipients. |
| `SMTP_USE_TLS` | `true` | STARTTLS before login. |

**Validation is at startup, not at alert time.** A typo in `ALERT_PROVIDER` or
`ALERT_MIN_LEVEL` fails the boot. The alternative — discovering it the first time
something breaks — is exactly the failure mode this system is meant to remove.

A provider that is *named* but *unconfigured* (Slack with no URL) is dropped with
a warning, and the console provider takes over so alerts are still recorded. The
system never goes silently mute. Use `ALERT_PROVIDER=null` when silence is the
intent.

---

## Severity

Four operational levels, ordered. `ALERT_MIN_LEVEL` filters on them.

| Level | Meaning | Persisted as |
|---|---|---|
| `INFO` | Noteworthy, no action. | `info` |
| `WARNING` | Degraded, self-recovering. | `medium` |
| `ERROR` | One unit of work failed. A bad document. | `high` |
| `CRITICAL` | The platform is unwell. Database, storage, queue, config. | `critical` |

The distinction that matters: **one awkward PDF is `ERROR`, an unreachable
database is `CRITICAL`.** Error codes `database_error`, `storage_error`,
`queue_error` and `configuration_error` escalate automatically, so a stage
failure caused by infrastructure does not arrive looking like a bad contract.

The filter applies to **delivery only**. The `alerts` row is always written —
losing it would defeat the Alerts screen.

---

## Categories

`document_processing`, `parser`, `ocr`, `ai_extraction`, `embedding`, `llm`,
`queue`, `worker_crash`, `scheduler`, `database`, `storage`,
`unhandled_exception`, `critical_system`.

Each carries a **suggested resolution** written for *this* system — "run
`make embedding-check`", "confirm `IDOC_API_KEY` is set" — rather than generic
advice. The pipeline stage picks the category automatically, so an embedding
failure says `embedding` and suggests the embedding probe.

---

## Payload

Every alert carries: timestamp, environment, project ID, document ID, job ID,
correlation ID, trace ID, stage, exception type, message, stack trace, worker
name, host name, retry count, severity, category and suggested resolution.

The trace ID is the one that pays for itself — it joins the alert to the request
in Jaeger without anyone grepping.

---

## Guarantees

**1. Alerting never changes the outcome of what raised it.** Every failure path
is caught and logged. This is not defensive habit: the service runs on the
orchestrator's terminal-failure path, and an exception there would replace an
accurate "enrichment failed because X" with a misleading alerting error —
destroying the evidence the operator needs. A database outage, a dead webhook and
a malformed error payload are all survivable; `AlertOutcome` reports honestly
what did and did not happen.

**2. Retries are bounded and backed off.** Exponential, capped. A batch of
documents failing together would otherwise become a burst that gets the
integration rate-limited exactly when it matters.

**3. A slow channel cannot hold up the pipeline.** Every attempt is wrapped in a
timeout; providers are dispatched concurrently, so several channels cost the
slowest rather than the sum.

**4. De-duplication is structural.** `dedupe_key` is
`category:document:stage`, unique among *open* alerts. A document that fails the
same stage on three retries produces one row with `occurrences: 3`, not three
rows to dismiss.

---

## Adding a channel

Implement `IAlertProvider` — `send()`, optionally `is_configured()` and
`aclose()`. Providers are formatters; retry, timeout, severity filtering and
failure suppression all live in `AlertDispatcher`, so a new channel cannot get
them subtly wrong.

```python
class PagerDutyProvider(IAlertProvider):
    name = "pagerduty"

    async def send(self, event: AlertEvent) -> None:
        ...  # raise on failure; the dispatcher handles the rest
```

Register it in `_build_provider` in `app/alerting/__init__.py` and add the name
to the validated set in `AlertSettings`. For anything that POSTs JSON, subclass
`HttpAlertProvider` and implement `build_payload` only.

---

## Usage

```python
# Terminal stage failure — what the orchestrator calls.
await AlertService(db).raise_processing_failure(
    contract=contract, stage=PipelineStage.EMBEDDING, error=error
)

# A live exception, with its traceback captured.
await AlertService(db).raise_exception(
    exc, category=AlertCategory.QUEUE, level=AlertLevel.CRITICAL
)
```

`AlertService(None)` is valid: a worker crash handler or scheduler tick with no
session still notifies, it just cannot persist.

---

## Testing

```bash
pytest tests/unit/test_alerting.py tests/unit/test_alert_service.py
pytest tests/integration/test_alert_providers.py
```

The provider tests wire the real provider, the real `httpx.AsyncClient` and the
real payload construction together, stopping only at the socket via
`httpx.MockTransport`. That level matters: provider bugs are almost always a
malformed body or a mishandled status code, and both survive a test that mocks
the client away.
