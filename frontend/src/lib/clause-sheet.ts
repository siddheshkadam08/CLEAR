/**
 * Reading and writing the Clause Master sheet.
 *
 * Both formats are handled here rather than on the server, for two reasons: the
 * `xlsx` package is already a dependency (declared, and until now imported
 * nowhere), and the backend has only `xlsxwriter` — it can write a workbook but
 * cannot read one. Parsing here means CSV and XLSX collapse to the same rows
 * before anything is sent.
 *
 * The browser's parse is a *convenience*, never the authority. The API validates
 * every field again and reports per-row failures, because what lands here decides
 * which clauses future uploads are checked for.
 *
 * Export and import use identical columns, so the file a user downloads is the
 * file they can edit and send straight back.
 */

import * as XLSX from 'xlsx';

import type { ClauseImportRow } from '@/api/types';

/** Column order in the generated sheet. Also the order the parser expects. */
const COLUMNS = [
  'agreement_type',
  'clause_key',
  'name',
  'description',
  'group_name',
  'synonyms',
  'is_active',
  'is_mandatory',
  'display_order',
] as const;

/** Human headers, so the file opens legibly in Excel. */
const HEADERS: Record<(typeof COLUMNS)[number], string> = {
  agreement_type: 'Agreement Type',
  clause_key: 'Clause Key',
  name: 'Clause Name',
  description: 'Description',
  group_name: 'Group',
  synonyms: 'Synonyms (semicolon separated)',
  is_active: 'Active',
  is_mandatory: 'Mandatory',
  display_order: 'Order',
};

type SheetRow = Record<string, string | number>;

function toSheetRows(rows: ClauseImportRow[]): SheetRow[] {
  return rows.map((row) => ({
    [HEADERS.agreement_type]: row.agreement_type,
    [HEADERS.clause_key]: row.clause_key,
    [HEADERS.name]: row.name ?? '',
    [HEADERS.description]: row.description ?? '',
    [HEADERS.group_name]: row.group_name ?? '',
    // Semicolons, not commas: a synonym list inside a CSV cell would otherwise
    // need quoting that survives a round trip through Excel, and often does not.
    [HEADERS.synonyms]: (row.synonyms ?? []).join('; '),
    [HEADERS.is_active]: row.is_active ? 'yes' : 'no',
    [HEADERS.is_mandatory]: row.is_mandatory ? 'yes' : 'no',
    [HEADERS.display_order]: row.display_order ?? 100,
  }));
}

function download(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  URL.revokeObjectURL(url);
}

function stamp(): string {
  const now = new Date();
  const pad = (value: number) => String(value).padStart(2, '0');
  return `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}`;
}

export function exportClauseSheet(rows: ClauseImportRow[], format: 'csv' | 'xlsx'): void {
  const sheet = XLSX.utils.json_to_sheet(toSheetRows(rows), {
    header: COLUMNS.map((column) => HEADERS[column]),
  });

  if (format === 'csv') {
    const csv = XLSX.utils.sheet_to_csv(sheet);
    // BOM so Excel opens UTF-8 correctly. Without it, a clause name with an
    // en-dash or a curly quote — which contract vocabulary is full of — renders
    // as mojibake, and the user's round trip corrupts the data.
    download(
      new Blob(['﻿', csv], { type: 'text/csv;charset=utf-8' }),
      `clause-master-${stamp()}.csv`,
    );
    return;
  }

  const book = XLSX.utils.book_new();
  XLSX.utils.book_append_sheet(book, sheet, 'Clause Master');
  const buffer = XLSX.write(book, { bookType: 'xlsx', type: 'array' }) as ArrayBuffer;
  download(
    new Blob([buffer], {
      type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    }),
    `clause-master-${stamp()}.xlsx`,
  );
}

/** Accepts the exported headers, the raw field names, or any case/spacing of either. */
function normaliseKey(header: string): string {
  return header.trim().toLowerCase().replace(/\s*\(.*?\)\s*/g, '').replace(/[^a-z0-9]+/g, '_');
}

const ALIASES: Record<string, (typeof COLUMNS)[number]> = {
  agreement_type: 'agreement_type',
  agreementtype: 'agreement_type',
  type: 'agreement_type',
  contract_type: 'agreement_type',
  doc_type: 'agreement_type',
  clause_key: 'clause_key',
  key: 'clause_key',
  clause: 'clause_key',
  clause_name: 'name',
  name: 'name',
  description: 'description',
  group: 'group_name',
  group_name: 'group_name',
  synonyms: 'synonyms',
  active: 'is_active',
  is_active: 'is_active',
  mandatory: 'is_mandatory',
  is_mandatory: 'is_mandatory',
  order: 'display_order',
  display_order: 'display_order',
};

function truthy(value: unknown): boolean {
  if (typeof value === 'boolean') return value;
  if (typeof value === 'number') return value !== 0;
  const text = String(value ?? '').trim().toLowerCase();
  return ['yes', 'y', 'true', '1', 'active', 'on'].includes(text);
}

export interface ParsedSheet {
  rows: ClauseImportRow[];
  /** Headers the parser did not recognise, so a mis-shaped file says so. */
  unknownColumns: string[];
}

/**
 * Parse an uploaded .csv or .xlsx into import rows.
 *
 * Throws only when the file is unreadable or has no recognisable key columns —
 * that is a wrong file, not a bad row. Individual bad rows are left for the
 * server to report, so one typo does not cost the other 399 rows.
 */
export async function parseClauseSheet(file: File): Promise<ParsedSheet> {
  const buffer = await file.arrayBuffer();
  const book = XLSX.read(buffer, { type: 'array' });
  const first = book.SheetNames[0];
  if (!first) throw new Error('That file has no sheets.');

  const raw = XLSX.utils.sheet_to_json<Record<string, unknown>>(book.Sheets[first]!, {
    defval: '',
  });
  if (!raw.length) throw new Error('That sheet has no rows.');

  const unknown = new Set<string>();
  const rows: ClauseImportRow[] = [];

  for (const record of raw) {
    const mapped: Partial<Record<(typeof COLUMNS)[number], unknown>> = {};
    for (const [header, value] of Object.entries(record)) {
      const column = ALIASES[normaliseKey(header)];
      if (column) mapped[column] = value;
      else if (String(header).trim()) unknown.add(String(header).trim());
    }

    const agreementType = String(mapped.agreement_type ?? '').trim();
    const clauseKey = String(mapped.clause_key ?? '').trim().toLowerCase();
    if (!agreementType || !clauseKey) continue;

    const order = Number(mapped.display_order);
    rows.push({
      agreement_type: agreementType,
      clause_key: clauseKey,
      name: String(mapped.name ?? '').trim() || null,
      description: String(mapped.description ?? '').trim() || null,
      group_name: String(mapped.group_name ?? '').trim() || null,
      synonyms: String(mapped.synonyms ?? '')
        .split(/[;|]/)
        .map((value) => value.trim())
        .filter(Boolean),
      // Absent means active. A sheet that omits the column is saying "these are
      // the clauses I want", not "switch them all off".
      is_active: mapped.is_active === undefined || mapped.is_active === ''
        ? true
        : truthy(mapped.is_active),
      is_mandatory: truthy(mapped.is_mandatory),
      display_order: Number.isFinite(order) && order > 0 ? order : null,
    });
  }

  if (!rows.length) {
    throw new Error(
      'No usable rows. Each row needs an agreement type and a clause key — ' +
        'export the current sheet to see the expected columns.',
    );
  }
  return { rows, unknownColumns: [...unknown] };
}
