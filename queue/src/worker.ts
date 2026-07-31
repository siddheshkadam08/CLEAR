/**
 * The worker shim - the whole point of §1.2.
 *
 * A worker does exactly three things:
 *
 *   1. take a message off a stage queue,
 *   2. POST it to the Python stage endpoint,
 *   3. translate the reply into a BullMQ outcome.
 *
 * It contains **no** business logic. It does not know what a contract is, what a
 * stage does, which stage runs next, or whether a failure is worth retrying. Python
 * decides all of that and says so in the response body; this file only honours the
 * decision. That is what keeps the queue swappable - the same contract is
 * implementable in arq or Celery in a few dozen lines.
 *
 * The one judgement made here is the distinction between a **stage failure** (a 200
 * carrying `status: failed`, already recorded by Python, retried only if Python said
 * to) and a **transport failure** (the call never got through, so nothing was
 * recorded and the dispatcher must retry on its own initiative). Conflating those
 * two would either double-run stages or silently drop them.
 */

import { Worker, type Job } from 'bullmq';
import { request } from 'undici';

import { type Stage, concurrencyFor, config, queueId, queueName, queueOptions } from './config.js';
import { logger } from './logger.js';
import {
  dispatchDuration,
  jobsCompleted,
  jobsDeadLettered,
  jobsDispatched,
  jobsRetried,
} from './metrics.js';
import { createConnection, deadLetterQueue } from './queues.js';

/** The queue message. Identifiers only - never document content. */
export interface StageMessage {
  job_id: string;
  contract_id: string;
  project_id: string;
  stage: Stage;
  attempt?: number;
  priority?: string;
  trace?: Record<string, string>;
  continue_pipeline?: boolean;
  options?: Record<string, unknown>;
}

/** What Python replies with. `should_retry` is the entire retry contract. */
interface StageResult {
  job_id: string;
  stage: string;
  status: string;
  attempt: number;
  duration_ms: number;
  next_stage: string | null;
  should_retry: boolean;
  retry_delay_ms: number;
  error?: { code?: string; message?: string } | null;
  warnings?: string[];
}

/**
 * Raised when the stage endpoint could not be reached or did not answer usefully.
 *
 * Distinct from a stage failure: nothing was recorded on the Python side, so this
 * is safe - and necessary - for BullMQ to retry.
 */
class TransportError extends Error {
  constructor(
    message: string,
    readonly statusCode?: number,
  ) {
    super(message);
    this.name = 'TransportError';
  }
}

/** Raised to make BullMQ retry after Python asked for it. */
class RetryableStageError extends Error {
  constructor(
    message: string,
    readonly delayMs: number,
  ) {
    super(message);
    this.name = 'RetryableStageError';
  }
}

async function dispatch(stage: Stage, message: StageMessage): Promise<StageResult> {
  const url = `${config.INTERNAL_API_BASE_URL}/internal/stages/${stage}/run`;
  const stopTimer = dispatchDuration.startTimer({ stage });

  let response;
  try {
    response = await request(url, {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        'x-internal-token': config.INTERNAL_API_TOKEN,
      },
      body: JSON.stringify(message),
      headersTimeout: config.QUEUE_STAGE_TIMEOUT_MS,
      bodyTimeout: config.QUEUE_STAGE_TIMEOUT_MS,
    });
  } catch (error) {
    stopTimer();
    throw new TransportError(
      `Could not reach the stage endpoint: ${(error as Error).message}`,
    );
  }

  const body = await response.body.text();
  stopTimer();

  if (response.statusCode >= 500) {
    // The service is unwell rather than the message being wrong; worth retrying.
    throw new TransportError(
      `The stage endpoint returned ${response.statusCode}: ${body.slice(0, 300)}`,
      response.statusCode,
    );
  }

  if (response.statusCode >= 400) {
    // A 4xx means this message is unacceptable and will be on every attempt.
    // Retrying would burn the budget to reach the same answer, so it goes straight
    // to the DLQ where a human can look at it.
    throw new Error(
      `The stage endpoint rejected the message (${response.statusCode}): ${body.slice(0, 300)}`,
    );
  }

  try {
    return JSON.parse(body) as StageResult;
  } catch {
    throw new TransportError(
      `The stage endpoint returned an unparseable body: ${body.slice(0, 200)}`,
    );
  }
}

/**
 * Move an exhausted job to the dead-letter queue.
 *
 * Retention would eventually trim BullMQ's own failed list, and a job that needs a
 * human decision must not disappear on a timer.
 */
async function deadLetter(
  stage: Stage,
  job: Job<StageMessage>,
  reason: string,
): Promise<void> {
  try {
    await deadLetterQueue.add(
      `${stage}:${job.data.job_id}`,
      {
        stage,
        message: job.data,
        reason,
        attempts_made: job.attemptsMade,
        failed_at: new Date().toISOString(),
        original_job_id: job.id,
      },
      // Hyphens, not colons. BullMQ reserves ':' as its Redis key separator and
      // throws `Custom Id cannot contain :` when constructing the job - so the DLQ
      // write itself failed, and the one record of an exhausted job was lost at the
      // exact moment it mattered. Same reservation that governs queue names.
      { jobId: `dlq-${stage}-${job.data.job_id}-${job.attemptsMade}` },
    );
    jobsDeadLettered.inc({ stage });
    logger.error(
      {
        stage,
        job_id: job.data.job_id,
        contract_id: job.data.contract_id,
        attempts: job.attemptsMade,
        reason,
      },
      'job_dead_lettered',
    );
  } catch (error) {
    // A DLQ write failure must not mask the original failure.
    logger.error(
      { err: error, stage, job_id: job.data.job_id },
      'dead_letter_write_failed',
    );
  }
}

export function createWorker(stage: Stage): Worker<StageMessage> {
  const concurrency = concurrencyFor(stage);

  const worker = new Worker<StageMessage>(
    queueId(stage),
    async (job: Job<StageMessage>) => {
      const message: StageMessage = {
        ...job.data,
        // BullMQ owns the attempt count; Python records it against the stage run,
        // so the two must agree.
        attempt: job.attemptsMade + 1,
      };

      jobsDispatched.inc({ stage });
      logger.info(
        {
          stage,
          job_id: message.job_id,
          contract_id: message.contract_id,
          attempt: message.attempt,
        },
        'stage_dispatched',
      );

      const result = await dispatch(stage, message);
      jobsCompleted.inc({ stage, status: result.status });

      if (result.should_retry) {
        // Python classified the failure as transient and chose the delay. The
        // dispatcher does not second-guess it.
        jobsRetried.inc({ stage, reason: 'python' });
        logger.warn(
          {
            stage,
            job_id: message.job_id,
            attempt: message.attempt,
            delay_ms: result.retry_delay_ms,
            error: result.error?.code,
          },
          'stage_retry_requested',
        );
        throw new RetryableStageError(
          result.error?.message ?? 'The stage asked to be retried.',
          result.retry_delay_ms,
        );
      }

      logger.info(
        {
          stage,
          job_id: message.job_id,
          status: result.status,
          duration_ms: result.duration_ms,
          next_stage: result.next_stage,
        },
        'stage_completed',
      );

      // The next stage is *not* enqueued here. Python's runner dispatches it, so
      // the pipeline's sequencing lives in one place rather than being split
      // across two languages (§1.2).
      return result;
    },
    {
      connection: createConnection(),
      concurrency,
      // Must match the producer's prefix exactly, or the worker watches a set of
      // Redis keys nobody writes to and every job sits in the queue forever.
      ...queueOptions,
      // A stage legitimately runs for minutes; the lock has to outlive it or BullMQ
      // would consider the job stalled and hand it to a second worker.
      lockDuration: config.QUEUE_STAGE_TIMEOUT_MS + 60_000,
      stalledInterval: 60_000,
      maxStalledCount: 2,
    },
  );

  worker.on('failed', (job, error) => {
    if (!job) {
      logger.error({ stage, err: error }, 'worker_failed_without_job');
      return;
    }

    const isTransport = error instanceof TransportError;
    if (isTransport) {
      jobsRetried.inc({ stage, reason: 'transport' });
    }

    const exhausted = job.attemptsMade >= (job.opts.attempts ?? config.QUEUE_MAX_ATTEMPTS);
    logger.warn(
      {
        stage,
        job_id: job.data?.job_id,
        attempts: job.attemptsMade,
        exhausted,
        transport: isTransport,
        err: error.message,
      },
      'stage_attempt_failed',
    );

    if (exhausted) {
      void deadLetter(stage, job, error.message);
    }
  });

  worker.on('error', (error) => {
    logger.error({ stage, err: error }, 'worker_error');
  });

  worker.on('stalled', (jobId) => {
    // Usually means a stage outran its lock, which is a tuning signal rather than
    // a bug: the work may still be running.
    logger.warn({ stage, job_id: jobId }, 'job_stalled');
  });

  logger.info({ stage, concurrency, queue: queueName(stage) }, 'worker_started');
  return worker;
}

export { RetryableStageError, TransportError };
