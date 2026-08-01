import type { ContractDetail, ContractKnowledge } from '@/api/types';
import { formatDate, humanise } from '@/lib/format';

function esc(value: unknown): string {
  const str = value == null ? '' : String(value);
  if (str.includes(',') || str.includes('"') || str.includes('\n')) {
    return `"${str.replace(/"/g, '""')}"`;
  }
  return str;
}

function row(...cells: unknown[]): string {
  return cells.map(esc).join(',');
}

function heading(title: string, count?: number): string {
  const label = count != null ? `${title} (${count})` : title;
  return `\n${row(label)}\n${row('-'.repeat(label.length))}`;
}

function table(headers: string[], rows: string[][]): string {
  const lines = [row(...headers)];
  for (const r of rows) lines.push(row(...r));
  return lines.join('\n');
}

function field(label: string, value: unknown): string {
  return row(label, value ?? '—');
}

export function exportContractCsv(contract: ContractDetail, knowledge: ContractKnowledge | null | undefined) {
  const lines: string[] = [];
  const metadata = contract.contract_metadata;
  const title = contract.title ?? contract.original_file_name;
  const now = new Date().toLocaleString();

  // Header
  lines.push(row('CONTRACT EXPORT REPORT'));
  lines.push(row(`Generated on: ${now}`));
  lines.push('');

  // ── Summary ──
  lines.push(heading('SUMMARY'));
  lines.push(field('Contract Title', title));
  lines.push(field('Agreement Type', humanise(contract.agreement_type)));
  lines.push(field('Status', humanise(contract.status)));
  lines.push(field('File Name', contract.original_file_name));
  lines.push(field('Pages', contract.page_count));
  lines.push('');

  // ── Parties & Commercial Terms ──
  lines.push(heading('COMMERCIAL TERMS'));
  lines.push(field('Party A', metadata?.party_a));
  lines.push(field('Party B', metadata?.party_b));
  lines.push(field('Contract Value', metadata?.contract_value != null
    ? `${metadata.currency ?? 'USD'} ${Number(metadata.contract_value).toLocaleString()}`
    : null));
  lines.push(field('Effective Date', formatDate(metadata?.effective_date)));
  lines.push(field('Expiration Date', formatDate(metadata?.expiration_date)));
  lines.push(field('Term', metadata?.term_months != null ? `${metadata.term_months} months` : null));
  lines.push(field('Payment Terms', metadata?.payment_terms_days != null ? `${metadata.payment_terms_days} days` : null));
  lines.push(field('Auto Renewal', metadata?.auto_renewal ? 'Yes' : 'No'));

  if (knowledge) {
    // ── Risk Assessment ──
    lines.push('');
    lines.push(heading('RISK ASSESSMENT'));
    lines.push(field('Overall Score', knowledge.assessment.score));
    lines.push(field('Risk Band', humanise(knowledge.assessment.band).toUpperCase()));
    const bySev = knowledge.assessment.by_severity;
    if (Object.keys(bySev).length) {
      lines.push(field('Breakdown', Object.entries(bySev).map(([s, c]) => `${humanise(s)}: ${c}`).join('  |  ')));
    }
    if (knowledge.assessment.has_unlimited_liability) {
      lines.push(field('⚠ Unlimited Liability', 'Yes'));
    }
    if (knowledge.assessment.missing_mandatory_clauses.length) {
      lines.push(field('Missing Mandatory Clauses', knowledge.assessment.missing_mandatory_clauses.map(humanise).join(', ')));
    }

    // ── Risks Detail ──
    if (knowledge.assessment.risks.length) {
      lines.push('');
      lines.push(heading('RISKS', knowledge.assessment.risks.length));
      lines.push(table(
        ['#', 'Severity', 'Risk Type', 'Description', 'Recommendation', 'Omission'],
        knowledge.assessment.risks.map((r, i) => [
          String(i + 1),
          humanise(r.severity).toUpperCase(),
          humanise(r.risk_type),
          r.description,
          r.recommendation ?? '',
          r.is_omission ? 'Yes' : '',
        ]),
      ));
    }

    // ── Obligations ──
    if (knowledge.obligations.length) {
      lines.push('');
      lines.push(heading('OBLIGATIONS', knowledge.obligations.length));
      lines.push(table(
        ['#', 'Action', 'Responsible Party', 'Due Date', 'Due Description', 'Trigger Event', 'Recurring'],
        knowledge.obligations.map((o, i) => [
          String(i + 1),
          o.action,
          o.responsible_party ?? '',
          o.due_date ? formatDate(o.due_date) : '',
          o.due_description ?? '',
          o.trigger_event ?? '',
          o.is_recurring ? 'Yes' : '',
        ]),
      ));
    }

    // ── Key Dates ──
    if (knowledge.key_dates.length) {
      lines.push('');
      lines.push(heading('KEY DATES', knowledge.key_dates.length));
      lines.push(table(
        ['#', 'Date Type', 'Date', 'Expression', 'Description'],
        knowledge.key_dates.map((d, i) => [
          String(i + 1),
          humanise(d.date_type),
          d.date_value ? formatDate(d.date_value) : '',
          d.date_expression ?? '',
          d.description ?? '',
        ]),
      ));
    }

    // ── Parties ──
    if (knowledge.parties.length) {
      lines.push('');
      lines.push(heading('PARTIES', knowledge.parties.length));
      lines.push(table(
        ['#', 'Name', 'Legal Name', 'Role', 'Entity Type', 'Jurisdiction', 'Primary'],
        knowledge.parties.map((p, i) => [
          String(i + 1),
          p.name,
          p.legal_name ?? '',
          humanise(p.role),
          humanise(p.entity_type),
          p.jurisdiction ?? '',
          p.is_primary ? 'Yes' : '',
        ]),
      ));
    }

    // ── Clauses ──
    if (knowledge.clauses.length) {
      lines.push('');
      lines.push(heading('CLAUSES', knowledge.clauses.length));
      lines.push(table(
        ['#', 'Clause Type', 'Clause Number', 'Title', 'Text'],
        knowledge.clauses.map((c, i) => [
          String(i + 1),
          humanise(c.clause_type),
          c.clause_number ?? '',
          c.title ?? c.section_title ?? '',
          c.text,
        ]),
      ));
    }

    // ── Score Breakdown ──
    if (knowledge.assessment.breakdown.length) {
      lines.push('');
      lines.push(heading('RISK SCORE BREAKDOWN', knowledge.assessment.breakdown.length));
      lines.push(table(
        ['#', 'Finding', 'Severity', 'Clause Type', 'Weight', 'Applied', 'Omission'],
        knowledge.assessment.breakdown.map((b, i) => [
          String(i + 1),
          b.description,
          humanise(b.severity).toUpperCase(),
          b.clause_type ? humanise(b.clause_type) : '',
          String(b.weight),
          String(b.applied),
          b.is_omission ? 'Yes' : '',
        ]),
      ));
    }
  }

  // Footer
  lines.push('');
  lines.push(row('--- End of Report ---'));

  const csv = lines.join('\n');
  const blob = new Blob(['\uFEFF' + csv], { type: 'text/csv;charset=utf-8;' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `${title.replace(/[^a-zA-Z0-9_-]/g, '_')}_report.csv`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}
