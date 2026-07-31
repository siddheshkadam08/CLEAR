/**
 * Display formatting.
 *
 * Centralised because inconsistent date and money formatting across screens reads
 * as sloppiness in a product whose whole claim is precision.
 */

import { differenceInDays, format, fromUnixTime, parseISO } from 'date-fns';

export function formatDate(value?: string | number | null): string {
  if (value === null || value === undefined || value === '') return '—';
  try {
    const num = Number(value);
    const date = Number.isFinite(num) && String(value).match(/^\d+$/)
      ? fromUnixTime(num)
      : parseISO(String(value));
    return format(date, 'dd-MM-yyyy');
  } catch {
    return String(value);
  }
}

export function formatDateTime(value?: string | null): string {
  if (!value) return '—';
  try {
    return format(parseISO(value), 'd MMM yyyy, HH:mm');
  } catch {
    return value;
  }
}

export function formatDateTimeFull(value?: string | number | null): string {
  if (value === null || value === undefined || value === '') return '—';
  try {
    const num = Number(value);
    const date = Number.isFinite(num) ? fromUnixTime(num) : parseISO(String(value));
    return format(date, 'dd-MM-yyyy hh:mm:ss a');
  } catch {
    return String(value);
  }
}

export function daysUntil(value?: string | number | null): number | null {
  if (value === null || value === undefined || value === '') return null;
  try {
    const num = Number(value);
    const date = Number.isFinite(num) && String(value).match(/^\d+$/)
      ? fromUnixTime(num)
      : parseISO(String(value));
    return differenceInDays(date, new Date());
  } catch {
    return null;
  }
}

export function formatMoney(value?: number | null, currency?: string | null): string {
  if (value === null || value === undefined) return '—';
  try {
    return new Intl.NumberFormat('en-GB', {
      style: currency ? 'currency' : 'decimal',
      currency: currency ?? undefined,
      maximumFractionDigits: 0,
    }).format(value);
  } catch {
    // An unrecognised currency code must not blank the figure out.
    return `${value.toLocaleString()} ${currency ?? ''}`.trim();
  }
}

export function formatNumber(value: number): string {
  return new Intl.NumberFormat('en-GB').format(value);
}

/** `limitation_of_liability` -> `Limitation of liability`. */
export function humanise(value?: string | null): string {
  if (!value) return '—';
  const spaced = value.replace(/_/g, ' ');
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

export function formatDuration(ms?: number | null): string {
  if (ms === null || ms === undefined) return '—';
  if (ms < 1000) return `${ms} ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)} s`;
  return `${Math.floor(ms / 60_000)}m ${Math.round((ms % 60_000) / 1000)}s`;
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
}

export function formatPercent(value: number, digits = 0): string {
  return `${(value * 100).toFixed(digits)}%`;
}
