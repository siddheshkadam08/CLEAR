/**
 * Domain -> badge tint mappings.
 *
 * These live apart from the Badge component so a status has exactly one colour
 * across the whole product. A "high risk" chip that is rose on one screen and amber
 * on another teaches the reader nothing.
 *
 * (Separate file rather than co-located with the component because a module that
 * exports both components and plain functions breaks Fast Refresh.)
 */

import type { BadgeVariant } from '@/components/common/Badge';

/** Contract and job lifecycle. */
export const getStatusVariant = (status: string): BadgeVariant => {
  const map: Record<string, BadgeVariant> = {
    ready: 'success',
    completed: 'success',
    succeeded: 'success',
    active: 'success',
    resolved: 'success',
    approved: 'success',
    processing: 'info',
    running: 'info',
    queued: 'info',
    uploaded: 'info',
    acknowledged: 'info',
    pending: 'neutral',
    retrying: 'warning',
    needs_review: 'warning',
    paused: 'warning',
    open: 'warning',
    failed: 'danger',
    cancelled: 'neutral',
    expired: 'neutral',
    archived: 'neutral',
    dismissed: 'neutral',
    skipped: 'neutral',
  };
  return map[status?.toLowerCase()] ?? 'neutral';
};

/** Risk bands and severities share a scale, so they share a mapper. */
export const getRiskVariant = (band?: string | null): BadgeVariant => {
  const map: Record<string, BadgeVariant> = {
    critical: 'danger',
    high: 'danger',
    medium: 'warning',
    low: 'success',
    info: 'info',
  };
  return map[(band ?? '').toLowerCase()] ?? 'neutral';
};

/** `needs_review` -> `Needs Review`. */
export const formatStatusLabel = (status: string) =>
  (status ?? '')
    .split(/[_-]+/)
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(' ');
