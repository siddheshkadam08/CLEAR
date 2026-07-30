/**
 * Entry point: start the HTTP surface and one worker per stage.
 *
 * Shutdown is graceful and ordered. On SIGTERM the server stops accepting new
 * enqueues first, then workers are closed - which lets a stage that is mid-flight
 * finish rather than being killed part-way through writing rows. A stage
 * interrupted mid-write is exactly the case the pipeline's idempotency has to clean
 * up afterwards, so avoiding it is worth the extra seconds.
 */

import type { Server } from 'node:http';

import type { Worker } from 'bullmq';

import { STAGES, config } from './config.js';
import { logger } from './logger.js';
import { closeQueues } from './queues.js';
import { createServer } from './server.js';
import { createWorker } from './worker.js';

const workers: Worker[] = [];
let server: Server | undefined;

async function main(): Promise<void> {
  logger.info(
    {
      environment: config.NODE_ENV,
      redis: config.REDIS_URL.replace(/\/\/.*@/, '//***@'),
      backend: config.INTERNAL_API_BASE_URL,
      stages: STAGES.length,
    },
    'queue_starting',
  );

  for (const stage of STAGES) {
    workers.push(createWorker(stage));
  }

  server = createServer().listen(config.PORT, () => {
    logger.info({ port: config.PORT }, 'queue_listening');
  });
}

async function shutdown(signal: string): Promise<void> {
  logger.info({ signal }, 'queue_shutting_down');

  // Stop accepting work before draining, so nothing new arrives while closing.
  if (server) {
    await new Promise<void>((resolve) => server?.close(() => resolve()));
  }

  // `close()` waits for in-flight jobs, which is the point: a stage killed
  // mid-write leaves partial rows for the next run to clean up.
  await Promise.all(workers.map((worker) => worker.close()));
  await closeQueues();

  logger.info('queue_stopped');
  process.exit(0);
}

process.on('SIGTERM', () => void shutdown('SIGTERM'));
process.on('SIGINT', () => void shutdown('SIGINT'));

process.on('unhandledRejection', (reason) => {
  logger.error({ err: reason }, 'unhandled_rejection');
});
process.on('uncaughtException', (error) => {
  // Unsafe to continue: state is unknown. Exit and let the orchestrator restart.
  logger.fatal({ err: error }, 'uncaught_exception');
  process.exit(1);
});

main().catch((error: unknown) => {
  logger.fatal({ err: error }, 'queue_failed_to_start');
  process.exit(1);
});
