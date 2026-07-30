/**
 * Queue and connection management.
 *
 * One BullMQ queue per pipeline stage, plus a dead-letter queue. Stages get their
 * own queues so a slow AI stage cannot head-of-line block a cheap validation, and
 * so each pool scales on its own curve.
 */

import { Queue, QueueEvents } from 'bullmq';
import { Redis } from 'ioredis';

import { DLQ_ID, STAGES, type Stage, config, queueId, queueOptions } from './config.js';
import { logger } from './logger.js';

/**
 * BullMQ requires `maxRetriesPerRequest: null` on the connection its blocking
 * commands use; with a retry limit the blocking read is aborted and workers stall
 * silently. Documented because the symptom - a worker that simply stops taking
 * jobs, with no error - is otherwise very hard to attribute.
 */
export function createConnection(): Redis {
  const connection = new Redis(config.REDIS_URL, {
    maxRetriesPerRequest: null,
    enableReadyCheck: true,
    lazyConnect: false,
  });

  connection.on('error', (error: Error) => {
    logger.error({ err: error }, 'redis_connection_error');
  });
  connection.on('reconnecting', () => {
    logger.warn('redis_reconnecting');
  });

  return connection;
}

const connection = createConnection();

/** Default job options. Retention keeps the admin view useful without unbounded growth. */
const defaultJobOptions = {
  attempts: config.QUEUE_MAX_ATTEMPTS,
  backoff: { type: 'exponential' as const, delay: config.QUEUE_BACKOFF_MS },
  removeOnComplete: { count: config.QUEUE_KEEP_COMPLETED },
  removeOnFail: { count: config.QUEUE_KEEP_FAILED },
};

const queues = new Map<Stage, Queue>();

for (const stage of STAGES) {
  queues.set(
    stage,
    new Queue(queueId(stage), { connection, defaultJobOptions, ...queueOptions }),
  );
}

/**
 * The dead-letter queue.
 *
 * Not a BullMQ failure list: those are per-queue and get trimmed by retention. A
 * job that exhausted its attempts is a thing an operator has to *decide* about, so
 * it is moved somewhere durable where it can be listed, inspected and replayed.
 */
export const deadLetterQueue = new Queue(DLQ_ID, {
  connection,
  ...queueOptions,
  defaultJobOptions: {
    // Never auto-retried. A DLQ entry is replayed deliberately or not at all.
    attempts: 1,
    removeOnComplete: false,
    removeOnFail: false,
  },
});

export function getQueue(stage: Stage): Queue {
  const queue = queues.get(stage);
  if (!queue) {
    throw new Error(`No queue is configured for stage '${stage}'.`);
  }
  return queue;
}

export function allQueues(): ReadonlyMap<Stage, Queue> {
  return queues;
}

/** Queue-level events, for depth metrics and dispatch logging. */
export function createQueueEvents(stage: Stage): QueueEvents {
  return new QueueEvents(queueId(stage), { connection: createConnection(), ...queueOptions });
}

export async function closeQueues(): Promise<void> {
  await Promise.all([...queues.values()].map((queue) => queue.close()));
  await deadLetterQueue.close();
  await connection.quit();
}

export { connection };
