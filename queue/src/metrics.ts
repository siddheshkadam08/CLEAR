/**
 * Prometheus metrics for the dispatch layer.
 *
 * Deliberately about *dispatch*, not about contracts: how long a job waited, how
 * often a call failed in transport, how deep each queue is. Anything about what a
 * stage did is Python's to report - duplicating it here would give two sources of
 * truth that drift.
 */

import { Counter, Gauge, Histogram, Registry, collectDefaultMetrics } from 'prom-client';

export const registry = new Registry();
collectDefaultMetrics({ register: registry, prefix: 'cip_queue_' });

export const jobsDispatched = new Counter({
  name: 'cip_queue_jobs_dispatched_total',
  help: 'Stage messages handed to the Python service.',
  labelNames: ['stage'] as const,
  registers: [registry],
});

export const jobsCompleted = new Counter({
  name: 'cip_queue_jobs_completed_total',
  help: 'Stage executions by reported outcome.',
  labelNames: ['stage', 'status'] as const,
  registers: [registry],
});

export const jobsRetried = new Counter({
  name: 'cip_queue_jobs_retried_total',
  help: 'Retries, split by who asked for them.',
  // `python` = the service classified the failure as retryable.
  // `transport` = the call itself did not get through.
  labelNames: ['stage', 'reason'] as const,
  registers: [registry],
});

export const jobsDeadLettered = new Counter({
  name: 'cip_queue_jobs_dead_lettered_total',
  help: 'Jobs that exhausted their attempts and were moved to the DLQ.',
  labelNames: ['stage'] as const,
  registers: [registry],
});

export const dispatchDuration = new Histogram({
  name: 'cip_queue_dispatch_duration_seconds',
  help: 'Round-trip time of the call into the Python stage endpoint.',
  labelNames: ['stage'] as const,
  buckets: [0.5, 1, 5, 15, 30, 60, 120, 300, 600, 1800],
  registers: [registry],
});

export const queueDepth = new Gauge({
  name: 'cip_queue_depth',
  help: 'Messages in a queue, by state.',
  labelNames: ['queue', 'state'] as const,
  registers: [registry],
});

export const enqueueFailures = new Counter({
  name: 'cip_queue_enqueue_failures_total',
  help: 'Enqueue attempts the dispatcher could not accept.',
  labelNames: ['stage'] as const,
  registers: [registry],
});
