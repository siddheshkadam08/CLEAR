/**
 * Clause tab body.
 *
 * The layout is driven entirely by the Clause Master's `ui_config`, delivered as a
 * `ClauseTab`: which fields lead, what a dropdown offers, and which attribute
 * values mark a clause for attention. Adding a dedicated tab is a configuration
 * change, not a frontend change.
 *
 * Two behaviours here carry weight beyond presentation:
 *
 * - A tab whose category is mandatory but had nothing extracted renders as an
 *   explicit absence, not as an empty list. "No limitation of liability clause was
 *   found" is one of the most consequential findings this product can produce, and
 *   an empty panel reads as "not loaded yet".
 * - Corrections go through the review endpoint, which records them as corrections
 *   against the model's original output rather than overwriting it. The reviewer
 *   sees what the model said and what it was changed to.
 */

import { useMutation, useQueryClient } from '@tanstack/react-query';
import { FileSearch, FileWarning, ShieldAlert } from 'lucide-react';
import { useState } from 'react';

import { knowledge as knowledgeApi } from '@/api/endpoints';
import { errorMessage } from '@/api/errors';
import type { BoundingBox, Clause, ClauseTab, UUID } from '@/api/types';
import { Badge } from '@/components/common/Badge';
import { ErrorBanner, NoticeBanner } from '@/components/common/Banner';
import { Button } from '@/components/common/Button';
import { Card } from '@/components/common/Card';
import { EmptyState } from '@/components/common/EmptyState';
import { inputClasses } from '@/components/common/Field';
import { formatPercent, humanise } from '@/lib/format';

export interface ClausePanelProps {
  contractId: UUID;
  tab: ClauseTab;
  onShowEvidence: (boxes: BoundingBox[], page?: number | null) => void;
}

export function ClausePanel({ contractId, tab, onShowEvidence }: ClausePanelProps) {
  if (tab.is_missing || tab.clauses.length === 0) {
    return (
      <EmptyState
        icon={tab.is_missing ? FileWarning : FileSearch}
        title={
          tab.is_missing
            ? `No ${tab.label.toLowerCase()} clause found in this contract`
            : `No ${tab.label.toLowerCase()} clause extracted`
        }
        description={
          tab.is_missing
            ? 'This category is mandatory for this document type. Its absence is treated as a risk and is reflected in the contract risk score. Check the source document before acting on this — if the clause is present but worded unusually, reprocess from extraction.'
            : 'Nothing was extracted for this category.'
        }
      />
    );
  }

  return (
    <div className="space-y-4">
      {tab.clauses.map((clause) => (
        <ClauseCard
          key={clause.id}
          contractId={contractId}
          clause={clause}
          tab={tab}
          onShowEvidence={onShowEvidence}
        />
      ))}
    </div>
  );
}

function ClauseCard({
  contractId,
  clause,
  tab,
  onShowEvidence,
}: {
  contractId: UUID;
  clause: Clause;
  tab: ClauseTab;
  onShowEvidence: (boxes: BoundingBox[], page?: number | null) => void;
}) {
  const queryClient = useQueryClient();
  const [expanded, setExpanded] = useState(false);
  const [draft, setDraft] = useState<Record<string, unknown>>({});
  const [note, setNote] = useState('');
  const [saveError, setSaveError] = useState<string | null>(null);

  const review = useMutation({
    mutationFn: (status: string) =>
      knowledgeApi.reviewClause(contractId, clause.id, {
        review_status: status,
        attributes: Object.keys(draft).length ? { ...clause.attributes, ...draft } : undefined,
        note: note.trim() || undefined,
      }),
    onSuccess: async () => {
      setDraft({});
      setNote('');
      setSaveError(null);
      await queryClient.invalidateQueries({ queryKey: ['knowledge', contractId] });
      await queryClient.invalidateQueries({ queryKey: ['contract', contractId] });
    },
    onError: (caught) => setSaveError(errorMessage(caught)),
  });

  const attributes = { ...clause.attributes, ...draft };
  const flagged = isHighlighted(attributes, tab.highlight_when);
  const confidence = clause.provenance?.confidence;
  const dirty = Object.keys(draft).length > 0 || note.trim().length > 0;

  const primary = tab.primary_fields.length
    ? tab.primary_fields
    : Object.keys(clause.attributes).slice(0, 6);
  const secondary = Object.keys(clause.attributes).filter((key) => !primary.includes(key));

  return (
    <Card className={flagged ? 'border-l-4 border-l-amber-400' : ''}>
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            {clause.clause_number ? (
              <span className="rounded-lg bg-slate-100 px-2 py-0.5 font-mono text-xs text-slate-600">
                {clause.clause_number}
              </span>
            ) : null}
            <span className="font-semibold text-slate-900">
              {clause.title ?? clause.section_title ?? tab.label}
            </span>
          </div>
          {clause.page_start ? (
            <p className="mt-1 text-xs text-slate-500">
              Page {clause.page_start}
              {clause.page_end && clause.page_end !== clause.page_start
                ? `–${clause.page_end}`
                : ''}
            </p>
          ) : null}
        </div>

        <div className="flex shrink-0 flex-wrap items-center gap-2">
          {confidence !== null && confidence !== undefined ? (
            <Badge
              text={formatPercent(confidence)}
              variant={
                confidence >= 0.85 ? 'success' : confidence >= 0.6 ? 'warning' : 'danger'
              }
              title="Extraction confidence"
            />
          ) : null}
          {clause.review_status && clause.review_status !== 'pending' ? (
            <Badge text={humanise(clause.review_status)} variant="neutral" />
          ) : null}
          {clause.bounding_boxes.length ? (
            <Button
              variant="secondary"
              size="sm"
              icon={FileSearch}
              onClick={() => onShowEvidence(clause.bounding_boxes, clause.page_start)}
            >
              Show in document
            </Button>
          ) : null}
        </div>
      </div>

      {flagged ? (
        <div className="mt-3">
          <NoticeBanner
            message={describeHighlight(attributes, tab.highlight_when, tab.label)}
          />
        </div>
      ) : null}

      {/* The dropdown is the cap basis: an enumerated commercial term that a
          reviewer must be able to correct in one action, because it drives the
          risk score and the portfolio-wide "unlimited liability" filter. */}
      {tab.dropdown ? (
        <label className="mt-4 block max-w-xs space-y-1.5">
          <span className="text-xs font-semibold uppercase tracking-wider text-slate-400">
            {humanise(tab.dropdown.field)}
          </span>
          <select
            value={String(attributes[tab.dropdown.field] ?? '')}
            onChange={(event) =>
              setDraft((current) => ({
                ...current,
                [tab.dropdown!.field]: event.target.value || null,
              }))
            }
            className={inputClasses}
          >
            <option value="">Not specified</option>
            {tab.dropdown.options.map((option) => (
              <option key={option} value={option}>
                {humanise(option)}
              </option>
            ))}
          </select>
        </label>
      ) : null}

      <dl className="mt-4 grid grid-cols-1 gap-4 sm:grid-cols-2">
        {primary.map((field) => (
          <AttributeValue
            key={field}
            field={field}
            value={attributes[field]}
            emphasised
            onToggle={
              typeof attributes[field] === 'boolean'
                ? (value) => setDraft((current) => ({ ...current, [field]: value }))
                : undefined
            }
          />
        ))}
      </dl>

      {clause.issues.length ? (
        <ul className="mt-4 space-y-1.5">
          {clause.issues.map((issue, index) => (
            <li
              key={index}
              className={[
                'flex items-start gap-2 rounded-xl px-3 py-2 text-xs leading-5',
                issue.severity === 'error'
                  ? 'bg-rose-50 text-rose-700'
                  : 'bg-amber-50 text-amber-700',
              ].join(' ')}
            >
              <ShieldAlert className="mt-0.5 h-3.5 w-3.5 shrink-0" />
              {issue.message}
            </li>
          ))}
        </ul>
      ) : null}

      {clause.summary ? (
        <p className="mt-4 text-sm leading-6 text-slate-700">{clause.summary}</p>
      ) : null}

      <button
        type="button"
        onClick={() => setExpanded((value) => !value)}
        className="mt-4 text-sm font-medium text-blue-600 transition hover:text-blue-700"
      >
        {expanded ? 'Hide clause text' : 'Show clause text'}
        {secondary.length
          ? ` and ${secondary.length} more field${secondary.length === 1 ? '' : 's'}`
          : ''}
      </button>

      {expanded ? (
        <div className="mt-4 space-y-4 border-t border-slate-100 pt-4">
          {secondary.length ? (
            <dl className="grid grid-cols-1 gap-4 sm:grid-cols-2">
              {secondary.map((field) => (
                <AttributeValue key={field} field={field} value={attributes[field]} />
              ))}
            </dl>
          ) : null}

          <blockquote className="rounded-2xl border-l-4 border-slate-300 bg-slate-50 px-4 py-3 text-sm leading-7 text-slate-700">
            {clause.text}
          </blockquote>

          <label className="block space-y-1.5">
            <span className="text-xs font-semibold uppercase tracking-wider text-slate-400">
              Review note (optional)
            </span>
            <textarea
              rows={2}
              value={note}
              onChange={(event) => setNote(event.target.value)}
              placeholder="Why this was approved, rejected or corrected."
              className={`${inputClasses} resize-y`}
            />
          </label>

          {saveError ? <ErrorBanner message={saveError} /> : null}

          <div className="flex flex-wrap gap-2">
            <Button
              size="sm"
              busy={review.isPending}
              onClick={() => review.mutate(dirty ? 'corrected' : 'approved')}
            >
              {dirty ? 'Save correction' : 'Approve'}
            </Button>
            <Button
              variant="secondary"
              size="sm"
              busy={review.isPending}
              onClick={() => review.mutate('rejected')}
            >
              Reject
            </Button>
            {dirty ? (
              <Button
                variant="ghost"
                size="sm"
                onClick={() => {
                  setDraft({});
                  setNote('');
                }}
              >
                Discard changes
              </Button>
            ) : null}
          </div>
        </div>
      ) : null}
    </Card>
  );
}

function AttributeValue({
  field,
  value,
  emphasised,
  onToggle,
}: {
  field: string;
  value: unknown;
  emphasised?: boolean;
  onToggle?: (value: boolean) => void;
}) {
  return (
    <div className={emphasised ? 'rounded-xl bg-slate-50 px-3 py-2.5' : ''}>
      <dt className="text-xs font-semibold uppercase tracking-wider text-slate-400">
        {humanise(field)}
      </dt>
      <dd className="mt-1 text-sm text-slate-900">
        {onToggle ? (
          <label className="inline-flex items-center gap-2">
            <input
              type="checkbox"
              checked={value === true}
              onChange={(event) => onToggle(event.target.checked)}
              className="h-4 w-4 rounded border-slate-300 text-blue-600 focus:ring-blue-500"
            />
            {value === true ? 'Yes' : 'No'}
          </label>
        ) : (
          renderValue(value)
        )}
      </dd>
    </div>
  );
}

function renderValue(value: unknown) {
  // `null` is a real answer here - the schema requires every attribute key, so a
  // null means "the extractor looked and the contract is silent", which is not the
  // same as a key that was never produced.
  if (value === null || value === undefined) {
    return <span className="text-slate-400">Not specified</span>;
  }
  if (typeof value === 'boolean') return value ? 'Yes' : 'No';
  if (Array.isArray(value)) {
    if (value.length === 0) return <span className="text-slate-400">None</span>;
    return (
      <div className="flex flex-wrap gap-1.5">
        {value.map((entry, index) => (
          <span
            key={index}
            className="rounded-full bg-white px-2.5 py-0.5 text-xs font-medium text-slate-600 ring-1 ring-inset ring-slate-200"
          >
            {typeof entry === 'string' ? humanise(entry) : JSON.stringify(entry)}
          </span>
        ))}
      </div>
    );
  }
  if (typeof value === 'object') {
    return <span className="break-all font-mono text-xs">{JSON.stringify(value)}</span>;
  }
  if (typeof value === 'string') return humanise(value);
  return String(value);
}

function isHighlighted(
  attributes: Record<string, unknown>,
  highlightWhen: Record<string, unknown>,
): boolean {
  return Object.entries(highlightWhen).some(
    ([field, expected]) => attributes[field] === expected,
  );
}

function describeHighlight(
  attributes: Record<string, unknown>,
  highlightWhen: Record<string, unknown>,
  label: string,
): string {
  const hits = Object.entries(highlightWhen)
    .filter(([field, expected]) => attributes[field] === expected)
    .map(([field, expected]) => {
      // Carve-outs are called out in plain terms because the consequence is not
      // obvious from the field name: a tidy 1x cap with IP and confidentiality
      // carved out is unlimited exposure for the matters most likely to be claimed.
      if (field === 'has_carve_outs' && expected === true) {
        const carveOuts = attributes.carve_outs;
        const list =
          Array.isArray(carveOuts) && carveOuts.length
            ? ` (${carveOuts.map((entry) => humanise(String(entry))).join(', ')})`
            : '';
        return `liability is unlimited for carved-out matters${list}`;
      }
      if (field === 'cap_basis' && expected === 'uncapped') return 'liability is not capped';
      return `${humanise(field)} is ${humanise(String(expected))}`;
    });

  return `${label}: ${hits.join('; ')}.`;
}

export default ClausePanel;
