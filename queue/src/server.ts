/**
 * The dispatcher's HTTP surface.
 *
 * Three concerns, all cluster-internal:
 *
 * * `POST /enqueue` - what Python calls to queue a stage. The only write endpoint.
 * * `GET /metrics`, `GET /queues` - depth and throughput for monitoring.
 * * `GET/POST /dlq` - list and replay dead-lettered jobs, because a job that
 *   exhausted its retries needs a human decision and therefore a way to act on it.
 *
 * Every route requires the shared internal token. The service is not exposed
 * publicly, but a dispatcher that would enqueue work for anyone who can reach it is
 * one network misconfiguration away from being a problem.
 */

import express, { type NextFunction, type Request, type Response } from 'express';
import { z } from 'zod';

import { STAGES, type Stage, config, isStage, queueName } from './config.js';
import { logger } from './logger.js';
import { enqueueFailures, queueDepth, registry } from './metrics.js';
import { allQueues, deadLetterQueue, getQueue } from './queues.js';

const enqueueSchema = z.object({
  job_id: z.string().uuid(),
  contract_id: z.string().uuid(),
  project_id: z.string().uuid(),
  stage: z.enum(STAGES),
  attempt: z.number().int().min(1).max(100).optional(),
  priority: z.string().optional(),
  trace: z.record(z.string()).optional(),
  continue_pipeline: z.boolean().optional(),
  options: z.record(z.unknown()).optional(),
  delay_ms: z.number().int().min(0).max(86_400_000).optional(),
});

/** BullMQ priority: lower runs sooner. */
const PRIORITY_RANK: Record<string, number> = {
  urgent: 1,
  high: 2,
  normal: 3,
  low: 4,
};

function requireInternalToken(req: Request, res: Response, next: NextFunction): void {
  const token = req.header('x-internal-token');
  if (token !== config.INTERNAL_API_TOKEN) {
    logger.warn({ path: req.path, ip: req.ip }, 'internal_token_rejected');
    res.status(401).json({
      error: { code: 'unauthenticated', message: 'Invalid internal service token.' },
    });
    return;
  }
  next();
}

export function createServer(): express.Express {
  const app = express();
  app.disable('x-powered-by');
  app.use(express.json({ limit: '256kb' }));

  // Liveness stays open so the container probe does not need the secret.
  app.get('/healthz', (_req, res) => {
    res.json({ status: 'ok', service: config.OTEL_SERVICE_NAME });
  });

  app.get('/metrics', async (_req, res) => {
    await refreshQueueDepths();
    res.set('Content-Type', registry.contentType);
    res.send(await registry.metrics());
  });

  app.use(requireInternalToken);

  // ---------------------------------------------------------------- enqueue
  app.post('/enqueue', async (req, res) => {
    const parsed = enqueueSchema.safeParse(req.body);
    if (!parsed.success) {
      // A malformed message is rejected rather than queued: queuing it would defer
      // the failure to a worker that can do nothing useful with it.
      logger.warn({ issues: parsed.error.issues }, 'enqueue_rejected');
      res.status(400).json({
        error: {
          code: 'validation_error',
          message: 'The enqueue payload is not a valid stage message.',
          details: parsed.error.format(),
        },
      });
      return;
    }

    const { delay_ms: delayMs, ...message } = parsed.data;
    const stage = message.stage;

    try {
      const job = await getQueue(stage).add(`${stage}:${message.job_id}`, message, {
        delay: delayMs ?? 0,
        priority: PRIORITY_RANK[message.priority ?? 'normal'] ?? 3,
        // Deterministic id per (job, stage, attempt): a duplicate delivery of the
        // same enqueue request collapses onto one queued job rather than running
        // the stage twice.
        jobId: `${message.job_id}:${stage}:${message.attempt ?? 1}`,
      });

      logger.info(
        {
          stage,
          job_id: message.job_id,
          contract_id: message.contract_id,
          queue_job_id: job.id,
          delay_ms: delayMs ?? 0,
        },
        'stage_enqueued',
      );
      res.status(202).json({ job_id: job.id, queue: queueName(stage) });
    } catch (error) {
      enqueueFailures.inc({ stage });
      logger.error({ err: error, stage, job_id: message.job_id }, 'enqueue_failed');
      res.status(503).json({
        error: {
          code: 'queue_error',
          message: 'The job could not be queued.',
        },
      });
    }
  });

  // ----------------------------------------------------------------- queues
  app.get('/queues', async (_req, res) => {
    const stats = await Promise.all(
      [...allQueues().entries()].map(async ([stage, queue]) => {
        const counts = await queue.getJobCounts(
          'waiting',
          'active',
          'completed',
          'failed',
          'delayed',
        );
        return {
          queue: queueName(stage),
          stage,
          waiting: counts.waiting ?? 0,
          active: counts.active ?? 0,
          completed: counts.completed ?? 0,
          failed: counts.failed ?? 0,
          delayed: counts.delayed ?? 0,
        };
      }),
    );
    res.json({ queues: stats, dead_letter: await deadLetterQueue.getJobCounts('waiting') });
  });

  // -------------------------------------------------------------------- DLQ
  app.get('/dlq', async (req, res) => {
    const limit = Math.min(Number.parseInt(String(req.query.limit ?? '50'), 10) || 50, 200);
    const jobs = await deadLetterQueue.getJobs(['waiting', 'delayed'], 0, limit - 1);
    res.json({
      count: jobs.length,
      jobs: jobs.map((job) => ({
        id: job.id,
        stage: job.data?.stage,
        job_id: job.data?.message?.job_id,
        contract_id: job.data?.message?.contract_id,
        reason: job.data?.reason,
        attempts_made: job.data?.attempts_made,
        failed_at: job.data?.failed_at,
      })),
    });
  });

  app.get('/dlq/size', async (_req, res) => {
    const counts = await deadLetterQueue.getJobCounts('waiting', 'delayed');
    res.json({ size: (counts.waiting ?? 0) + (counts.delayed ?? 0) });
  });

  app.post('/dlq/:id/replay', async (req, res) => {
    const entry = await deadLetterQueue.getJob(req.params.id);
    if (!entry) {
      res.status(404).json({
        error: { code: 'not_found', message: 'No such dead-letter entry.' },
      });
      return;
    }

    const stage = entry.data?.stage;
    const message = entry.data?.message;
    if (!isStage(stage) || !message?.job_id) {
      res.status(422).json({
        error: {
          code: 'unprocessable',
          message: 'This dead-letter entry does not carry a replayable stage message.',
        },
      });
      return;
    }

    // Attempt 1 again: the retry budget is per *dispatch decision*, and an
    // operator replaying a job after fixing the cause is making a new one.
    const replayed = await getQueue(stage).add(
      `${stage}:${message.job_id}:replay`,
      { ...message, attempt: 1 },
      { jobId: `${message.job_id}:${stage}:replay:${Date.now()}` },
    );
    await entry.remove();

    logger.info(
      { stage, job_id: message.job_id, queue_job_id: replayed.id },
      'dead_letter_replayed',
    );
    res.status(202).json({ job_id: replayed.id, queue: queueName(stage) });
  });

  app.delete('/dlq/:id', async (req, res) => {
    const entry = await deadLetterQueue.getJob(req.params.id);
    if (!entry) {
      res.status(404).json({
        error: { code: 'not_found', message: 'No such dead-letter entry.' },
      });
      return;
    }
    await entry.remove();
    logger.info({ id: req.params.id }, 'dead_letter_discarded');
    res.status(204).send();
  });

  app.use((error: Error, _req: Request, res: Response, _next: NextFunction) => {
    logger.error({ err: error }, 'unhandled_request_error');
    res.status(500).json({
      error: { code: 'internal_error', message: 'An unexpected error occurred.' },
    });
  });

  return app;
}

/** Refresh the depth gauges. Called on scrape so they are never stale. */
async function refreshQueueDepths(): Promise<void> {
  for (const [stage, queue] of allQueues()) {
    try {
      const counts = await queue.getJobCounts('waiting', 'active', 'delayed', 'failed');
      const name = queueName(stage as Stage);
      queueDepth.set({ queue: name, state: 'waiting' }, counts.waiting ?? 0);
      queueDepth.set({ queue: name, state: 'active' }, counts.active ?? 0);
      queueDepth.set({ queue: name, state: 'delayed' }, counts.delayed ?? 0);
      queueDepth.set({ queue: name, state: 'failed' }, counts.failed ?? 0);
    } catch (error) {
      // A scrape must still return the metrics it can gather.
      logger.warn({ err: error, stage }, 'queue_depth_read_failed');
    }
  }
}
