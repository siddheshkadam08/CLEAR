/**
 * Date helpers, against the two shapes the API actually sends.
 *
 * The /jobs screen rendered `Embedding · started 1785762288.750538` next to a
 * correctly formatted `finished` on the same line. Both fields carry the same
 * kind of value - a *fractional* Unix timestamp - and the difference was only
 * which helper the JSX happened to call.
 *
 * The API is not consistent about this and cannot easily be made so: Pydantic
 * serialises some `datetime` fields as ISO strings and others as epoch seconds
 * with microseconds after the point, depending on the schema they pass through.
 * So every helper has to accept both, and the fractional part is the specific
 * thing that broke them:
 *
 * - `formatDateTime` took `string` only, so an epoch reached `parseISO`, threw,
 *   and its catch printed the raw value.
 * - `formatDate` and `daysUntil` guarded with `/^\d+$/`, which a decimal point
 *   fails, so those epochs also fell through to `parseISO`.
 *
 * These pin the behaviour rather than the wording of any one format string.
 */

import { describe, expect, it } from 'vitest';

import { daysUntil, formatDate, formatDateTime, formatDateTimeFull } from './format';

/** The exact value the /jobs screen printed raw. */
const EPOCH = 1785762288.750538;
/** The same instant, in the other shape the API uses. */
const ISO = '2026-08-03T13:04:48.750538Z';

describe('fractional Unix timestamps', () => {
  it('formatDateTime formats them instead of printing the number', () => {
    const rendered = formatDateTime(EPOCH);

    expect(rendered).not.toContain('1785762288');
    expect(rendered).toMatch(/^\d{1,2} \w{3} \d{4}, \d{2}:\d{2}$/);
  });

  it('an epoch and an ISO string of the same instant render identically', () => {
    // The /jobs regression in one line: `started` and `finished` disagreed only
    // because they took different code paths to the same kind of value.
    expect(formatDateTime(EPOCH)).toBe(formatDateTime(ISO));
    expect(formatDate(EPOCH)).toBe(formatDate(ISO));
    expect(formatDateTimeFull(EPOCH)).toBe(formatDateTimeFull(ISO));
  });

  it('formatDate accepts the decimal point', () => {
    expect(formatDate(EPOCH)).toMatch(/^\d{2}-\d{2}-\d{4}$/);
  });

  it('formatDateTimeFull keeps working, as it always did', () => {
    expect(formatDateTimeFull(EPOCH)).toMatch(
      /^\d{2}-\d{2}-\d{4} \d{2}:\d{2}:\d{2} (AM|PM)$/,
    );
  });

  it('daysUntil returns a number rather than null', () => {
    expect(typeof daysUntil(EPOCH)).toBe('number');
  });

  it('whole-second epochs work too', () => {
    expect(formatDateTime(1785762288)).toMatch(/^\d{1,2} \w{3} \d{4}, \d{2}:\d{2}$/);
  });
});

describe('ISO strings', () => {
  it('are unaffected', () => {
    expect(formatDate(ISO)).toMatch(/^\d{2}-\d{2}-\d{4}$/);
    expect(formatDateTime(ISO)).toMatch(/^\d{1,2} \w{3} \d{4}, \d{2}:\d{2}$/);
  });

  it('a date-only string is accepted', () => {
    expect(formatDate('2026-12-25')).toBe('25-12-2026');
  });
});

describe('absent and malformed values', () => {
  it.each([null, undefined, ''])('%p renders as a dash', (value) => {
    expect(formatDate(value)).toBe('—');
    expect(formatDateTime(value)).toBe('—');
    expect(formatDateTimeFull(value)).toBe('—');
    expect(daysUntil(value)).toBeNull();
  });

  it('an unparseable value falls back to itself rather than crashing', () => {
    // `format()` throws on an invalid date, so this has to be caught before it
    // reaches it - a thrown error here would blank the whole row.
    expect(formatDateTime('not-a-date')).toBe('not-a-date');
    expect(formatDate('not-a-date')).toBe('not-a-date');
    expect(daysUntil('not-a-date')).toBeNull();
  });
});
