/**
 * Display formatting.
 *
 * Centralised because inconsistent date and money formatting across screens reads
 * as sloppiness in a product whose whole claim is precision.
 */

import { differenceInDays, format, fromUnixTime, isValid, parseISO } from 'date-fns';

/** A timestamp as it can arrive from the API. */
export type DateInput = string | number | null | undefined;

/**
 * Parse a timestamp in either shape the API sends, or return null.
 *
 * The API is not consistent, and cannot easily be made so: Pydantic serialises
 * some `datetime` fields as ISO strings and others as **fractional** Unix
 * seconds - `1785762288.750538` - depending on the schema they pass through.
 * A single job row carries both.
 *
 * Every date helper below shares this, because the previous per-function
 * attempts disagreed in ways that only showed up on certain fields:
 *
 * - `formatDateTime` handled ISO only, so an epoch fell to its catch and the
 *   raw number was printed. That is the "started 1785762288.750538" on /jobs -
 *   and the same row's "finished" looked right, because it happened to be
 *   rendered by a different helper.
 * - `formatDate` and `daysUntil` tested `/^\d+$/`, which rejects the fractional
 *   part, so those same epochs fell through to `parseISO` and failed too.
 *
 * Returning null rather than throwing lets each caller choose its own fallback,
 * and `isValid` is checked here so a malformed value cannot reach `format()`,
 * which throws on an invalid date rather than returning a marker.
 */
function toDate(value: DateInput): Date | null {
  if (value === null || value === undefined || value === '') return null;
  // A number, or a string that is entirely numeric - `Number('')` is 0 and
  // `Number('2026-01-02')` is NaN, so both are excluded by construction.
  const numeric = typeof value === 'number' ? value : Number(value);
  const date =
    Number.isFinite(numeric) && String(value).trim() !== ''
      ? fromUnixTime(numeric)
      : parseISO(String(value));
  return isValid(date) ? date : null;
}

export function formatDate(value?: DateInput): string {
  const date = toDate(value);
  if (!date) return value === null || value === undefined || value === '' ? '—' : String(value);
  return format(date, 'dd-MM-yyyy');
}

export function formatDateTime(value?: DateInput): string {
  const date = toDate(value);
  if (!date) return value === null || value === undefined || value === '' ? '—' : String(value);
  return format(date, 'd MMM yyyy, HH:mm');
}

export function formatDateTimeFull(value?: DateInput): string {
  const date = toDate(value);
  if (!date) return value === null || value === undefined || value === '' ? '—' : String(value);
  return format(date, 'dd-MM-yyyy hh:mm:ss a');
}

export function daysUntil(value?: DateInput): number | null {
  const date = toDate(value);
  return date ? differenceInDays(date, new Date()) : null;
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

/**
 * Agreement types as a lawyer writes them.
 *
 * `humanise` only uppercases the first letter, which turns `msa` into "Msa" and
 * `license_agreement` into "License agreement" — legible, but wrong in a column
 * of contract types. Only the ones that need it are listed; anything else falls
 * through to `humanise`, so a type added to the backend taxonomy still renders
 * rather than showing a blank.
 */
const AGREEMENT_TYPE_LABELS: Record<string, string> = {
  msa: 'MSA',
  nda: 'NDA',
  sow: 'SOW',
  license_agreement: 'License Agreement',
  purchase_order: 'Purchase Order',
  service_agreement: 'Service Agreement',
  vendor_agreement: 'Vendor Agreement',
  employment_agreement: 'Employment Agreement',
  consulting_agreement: 'Consulting Agreement',
  partnership_agreement: 'Partnership Agreement',
  research_collaboration: 'Research Collaboration',
  government_contract: 'Government Contract',
  healthcare_agreement: 'Healthcare Agreement',
  insurance_policy: 'Insurance Policy',
};

export function formatAgreementType(value?: string | null): string {
  if (!value) return '—';
  return AGREEMENT_TYPE_LABELS[value] ?? humanise(value);
}

/**
 * Pipeline stages as the product names them.
 *
 * `docpipeline` is the stage's identifier everywhere it is durable — the queue
 * name, the `pipeline_stage` enum in the database, the reprocess argument — so it
 * cannot be renamed without a migration and a queue drain. What it *does* is
 * detect clauses, which is what the screen should say. This maps the one to the
 * other at the point of display and nowhere else.
 *
 * Same fallthrough as the agreement types above: a stage added to the backend
 * still renders via `humanise` rather than showing blank.
 */
const STAGE_LABELS: Record<string, string> = {
  docpipeline: 'Clause Detection',
};

export function formatStage(value?: string | null): string {
  if (!value) return '—';
  return STAGE_LABELS[value] ?? humanise(value);
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
