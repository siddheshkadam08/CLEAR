/**
 * Structured logging, matching the backend's JSON shape.
 *
 * One log format across Python and Node means a single query in the log store
 * follows a job across the boundary rather than stopping at it.
 */

import pino from 'pino';

import { config } from './config.js';

export const logger = pino({
  level: config.LOG_LEVEL,
  base: { service: config.OTEL_SERVICE_NAME },
  timestamp: pino.stdTimeFunctions.isoTime,
  formatters: {
    // `level: "info"` rather than `level: 30`, so it reads the same as the
    // backend's structlog output.
    level: (label) => ({ level: label }),
  },
  redact: {
    // The internal token travels in a header on every dispatch; it must never
    // reach the log store.
    paths: ['req.headers["x-internal-token"]', 'headers["x-internal-token"]', 'token'],
    censor: '[redacted]',
  },
});

export type Logger = typeof logger;
