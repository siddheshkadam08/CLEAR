/**
 * Configuration, entirely from the environment.
 *
 * Parsed and validated once at startup: a dispatcher that boots with a missing
 * Redis URL and only discovers it on the first job has already accepted work it
 * cannot deliver.
 */

import { z } from 'zod';

/**
 * Stage names, mirroring `app.core.enums.PipelineStage`.
 *
 * Duplicated deliberately - this is the *only* domain knowledge the dispatcher has,
 * and it is a list of queue names, not behaviour. Anything beyond "these queues
 * exist" belongs in Python (§1.2).
 */
export const STAGES = [
  'validation',
  'parser',
  'enrichment',
  'classification',
  'chunking',
  'ai_extraction',
  'embedding',
  'indexing',
] as const;

export type Stage = (typeof STAGES)[number];

const envSchema = z.object({
  NODE_ENV: z.string().default('development'),
  PORT: z.coerce.number().int().positive().default(9100),

  REDIS_URL: z.string().url().default('redis://localhost:6379/0'),
  QUEUE_PREFIX: z.string().min(1).default('cip'),

  /** Where the Python stage endpoints live. */
  INTERNAL_API_BASE_URL: z.string().url().default('http://localhost:8000'),
  INTERNAL_API_TOKEN: z.string().min(1),

  /**
   * Per-stage HTTP timeout. Generous by default: parsing a 300-page scanned
   * agreement legitimately takes minutes, and a timeout that fires mid-parse
   * produces a retry that redoes all of it.
   */
  QUEUE_STAGE_TIMEOUT_MS: z.coerce.number().int().positive().default(1_800_000),

  /**
   * Attempts *the dispatcher* makes. This is a transport-level backstop only:
   * Python decides whether a stage failure is worth retrying and says so in the
   * response. These attempts cover the case where the call never got through.
   */
  QUEUE_MAX_ATTEMPTS: z.coerce.number().int().min(1).max(20).default(3),
  QUEUE_BACKOFF_MS: z.coerce.number().int().positive().default(5_000),

  /** Completed/failed job retention, so the admin view has recent history. */
  QUEUE_KEEP_COMPLETED: z.coerce.number().int().nonnegative().default(1_000),
  QUEUE_KEEP_FAILED: z.coerce.number().int().nonnegative().default(5_000),

  OTEL_SERVICE_NAME: z.string().default('cip-queue'),
  LOG_LEVEL: z.string().default('info'),
});

const parsed = envSchema.safeParse(process.env);

if (!parsed.success) {
  // Written directly to stderr: the logger is not configured yet, and a config
  // failure must be visible even if nothing else starts.
  process.stderr.write(
    `Invalid queue configuration:\n${JSON.stringify(parsed.error.format(), null, 2)}\n`,
  );
  process.exit(1);
}

export const config = parsed.data;

/** Per-stage worker concurrency, from `WORKER_CONCURRENCY_<STAGE>`. */
const defaultConcurrency: Record<Stage, number> = {
  validation: 10,
  parser: 20,
  enrichment: 10,
  classification: 10,
  chunking: 20,
  ai_extraction: 10,
  embedding: 15,
  indexing: 5,
};

export function concurrencyFor(stage: Stage): number {
  const raw = process.env[`WORKER_CONCURRENCY_${stage.toUpperCase()}`];
  const parsedValue = raw ? Number.parseInt(raw, 10) : Number.NaN;
  return Number.isFinite(parsedValue) && parsedValue > 0
    ? parsedValue
    : defaultConcurrency[stage];
}

/**
 * The BullMQ queue name for a stage.
 *
 * Bare stage, no prefix: BullMQ uses `:` as its own Redis key separator and
 * rejects any queue name containing one - `new Queue('cip:validation')` throws
 * `Queue name cannot contain :` at construction, which crash-loops the whole
 * service before it ever accepts a job. The prefix belongs in BullMQ's own
 * `prefix` option, which is what {@link queueOptions} supplies; the resulting
 * Redis keys are `cip:validation:*` either way.
 */
export function queueId(stage: Stage): string {
  return stage;
}

/**
 * Display name for a stage's queue. Matches `IQueueClient.queue_name` in Python,
 * which uses it as a label in logs, stats and API responses - never as a Redis key.
 */
export function queueName(stage: Stage): string {
  return `${config.QUEUE_PREFIX}:${stage}`;
}

/** The dead-letter queue: jobs that exhausted their attempts. */
export const DLQ_ID = 'dlq';
export const DLQ_NAME = `${config.QUEUE_PREFIX}:dlq`;

/** Shared BullMQ options that put the prefix where BullMQ expects it. */
export const queueOptions = { prefix: config.QUEUE_PREFIX } as const;

export function isStage(value: unknown): value is Stage {
  return typeof value === 'string' && (STAGES as readonly string[]).includes(value);
}
