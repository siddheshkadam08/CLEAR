/**
 * Clause Master — which clauses each agreement type is checked for.
 *
 * The screen this replaces was a master/detail over 30 global clauses in which a
 * user could change exactly four things — mandatory, active, a confidence slider
 * and a priority integer — while being shown, read-only, two blocks of raw JSON
 * (`extraction_rule`, `output_schema`), five separate facts about `ui_config`,
 * and a rule-version badge whose history had no UI. There was no create, no
 * delete, and no way to see or change which clauses a given agreement type
 * actually looks for. That last one is the thing people needed.
 *
 * So: group by agreement type, one row per clause, and every control on the row.
 *
 * **Two objects, deliberately not blurred.** Toggling active, marking mandatory
 * and removing are *mappings* — they change this agreement type only. Editing a
 * name, description or synonyms changes the *clause*, everywhere it is used. The
 * row separates them: the switch and the remove button act here, the edit dialog
 * says plainly that it does not.
 *
 * **Active means new uploads.** Extraction reads the mapping once, while a
 * document is processed. Switching a clause off does not re-judge contracts
 * already extracted — their clauses are evidence of what those documents say. The
 * header states this rather than leaving it to be discovered.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  AlertTriangle,
  ChevronDown,
  ChevronRight,
  Download,
  Pencil,
  Plus,
  Search,
  Trash2,
  Upload,
  X,
} from 'lucide-react';
import { useMemo, useRef, useState } from 'react';

import { clauseMaster as clauseApi } from '@/api/endpoints';
import { errorMessage } from '@/api/errors';
import type { AgreementClause, AgreementTypeClauses, ClauseImportResult } from '@/api/types';
import { Badge } from '@/components/common/Badge';
import { ErrorBanner, NoticeBanner } from '@/components/common/Banner';
import { Button } from '@/components/common/Button';
import { Card, PageHeader } from '@/components/common/Card';
import { EmptyState } from '@/components/common/EmptyState';
import { inputClasses } from '@/components/common/Field';
import { LoadingSpinner } from '@/components/common/LoadingSpinner';
import { Modal } from '@/components/common/Modal';
import { useCanAdminister } from '@/lib/auth';
import { exportClauseSheet, parseClauseSheet } from '@/lib/clause-sheet';

type ParsedSheet = Awaited<ReturnType<typeof parseClauseSheet>>;

export function ClauseMasterPage() {
  const queryClient = useQueryClient();
  const isAdmin = useCanAdminister();

  const [query, setQuery] = useState('');
  const [openTypes, setOpenTypes] = useState<Set<string>>(new Set());
  const [editing, setEditing] = useState<{ clause: AgreementClause | null } | null>(null);
  const [importing, setImporting] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  const groups = useQuery({
    queryKey: ['clause-master'],
    queryFn: () => clauseApi.byAgreementType(true),
  });

  const invalidate = () => queryClient.invalidateQueries({ queryKey: ['clause-master'] });

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    const source = groups.data ?? [];
    if (!needle) return source;
    return source
      .map((group) => ({
        ...group,
        clauses: group.clauses.filter(
          (clause) =>
            clause.name.toLowerCase().includes(needle) ||
            clause.clause_key.includes(needle) ||
            clause.synonyms.some((value) => value.toLowerCase().includes(needle)),
        ),
      }))
      .filter((group) => group.clauses.length || group.label.toLowerCase().includes(needle));
  }, [groups.data, query]);

  async function onExport(format: 'csv' | 'xlsx') {
    try {
      exportClauseSheet(await clauseApi.exportRows(), format);
      setActionError(null);
    } catch (caught) {
      setActionError(errorMessage(caught));
    }
  }

  return (
    <div className="space-y-5">
      <PageHeader
        title="Clause Master"
        subtitle="Which clauses each agreement type is checked for. Changes apply to new uploads; contracts already processed keep the clauses they were extracted with."
        actions={
          isAdmin ? (
            <div className="flex flex-wrap gap-2">
              <Button
                variant="secondary"
                size="sm"
                icon={Download}
                onClick={() => void onExport('xlsx')}
              >
                XLSX
              </Button>
              <Button
                variant="secondary"
                size="sm"
                icon={Download}
                onClick={() => void onExport('csv')}
              >
                CSV
              </Button>
              <Button variant="secondary" size="sm" icon={Upload} onClick={() => setImporting(true)}>
                Import
              </Button>
              <Button size="sm" icon={Plus} onClick={() => setEditing({ clause: null })}>
                New clause
              </Button>
            </div>
          ) : null
        }
      />

      <Card dense>
        <div className="relative">
          <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-400" />
          <input
            type="search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Search clauses, keys or alternative headings"
            className={`${inputClasses} pl-9`}
          />
        </div>
        {!isAdmin ? (
          <p className="mt-2 text-xs text-slate-500">
            Read-only for you. A system administrator can change which clauses apply.
          </p>
        ) : null}
      </Card>

      {actionError ? <ErrorBanner message={actionError} /> : null}
      {groups.error ? (
        <ErrorBanner message={errorMessage(groups.error)} onRetry={() => void groups.refetch()} />
      ) : null}

      {groups.isLoading ? (
        <Card>
          <LoadingSpinner label="Loading the clause taxonomy..." />
        </Card>
      ) : filtered.length ? (
        <div className="space-y-3">
          {filtered.map((group) => (
            <AgreementTypeCard
              key={group.agreement_type}
              group={group}
              expanded={openTypes.has(group.agreement_type) || Boolean(query.trim())}
              onToggleExpanded={() =>
                setOpenTypes((current) => {
                  const next = new Set(current);
                  if (next.has(group.agreement_type)) next.delete(group.agreement_type);
                  else next.add(group.agreement_type);
                  return next;
                })
              }
              editable={isAdmin}
              onChanged={invalidate}
              onEditClause={(clause) => setEditing({ clause })}
              onError={setActionError}
            />
          ))}
        </div>
      ) : (
        <EmptyState
          icon={AlertTriangle}
          title={query ? 'Nothing matches that search' : 'No agreement types are configured'}
          description={
            query
              ? 'Try a clause name, its key, or one of its alternative headings.'
              : 'Agreement types come from the configured document profiles. Seed them to populate this screen.'
          }
        />
      )}

      {editing ? (
        <ClauseDialog
          clause={editing.clause}
          onClose={() => setEditing(null)}
          onSaved={async () => {
            setEditing(null);
            await invalidate();
          }}
        />
      ) : null}

      {importing ? (
        <ImportDialog onClose={() => setImporting(false)} onImported={invalidate} />
      ) : null}
    </div>
  );
}

// =============================================================================
// One agreement type
// =============================================================================
function AgreementTypeCard({
  group,
  expanded,
  onToggleExpanded,
  editable,
  onChanged,
  onEditClause,
  onError,
}: {
  group: AgreementTypeClauses;
  expanded: boolean;
  onToggleExpanded: () => void;
  editable: boolean;
  onChanged: () => void | Promise<unknown>;
  onEditClause: (clause: AgreementClause) => void;
  onError: (message: string | null) => void;
}) {
  const mapped = group.clauses.filter((clause) => clause.is_mapped);
  const active = mapped.filter((clause) => clause.is_active);
  const mandatory = active.filter((clause) => clause.is_mandatory);
  const available = group.clauses.filter((clause) => !clause.is_mapped);

  const setMapping = useMutation({
    mutationFn: (body: { clause_key: string; is_active: boolean; is_mandatory: boolean }) =>
      clauseApi.setMapping(group.agreement_type, body),
    onSuccess: () => {
      onError(null);
      void onChanged();
    },
    onError: (caught) => onError(errorMessage(caught)),
  });

  const removeMapping = useMutation({
    mutationFn: (clauseKey: string) => clauseApi.removeMapping(group.agreement_type, clauseKey),
    onSuccess: () => {
      onError(null);
      void onChanged();
    },
    onError: (caught) => onError(errorMessage(caught)),
  });

  const busy = setMapping.isPending || removeMapping.isPending;

  return (
    <Card className="overflow-hidden p-0">
      <button
        type="button"
        onClick={onToggleExpanded}
        aria-expanded={expanded}
        className="flex w-full items-center gap-3 px-5 py-4 text-left transition hover:bg-slate-50 dark:hover:bg-slate-800/60"
      >
        {expanded ? (
          <ChevronDown className="h-4 w-4 shrink-0 text-slate-400" />
        ) : (
          <ChevronRight className="h-4 w-4 shrink-0 text-slate-400" />
        )}
        <span className="min-w-0 flex-1">
          <span className="block font-semibold text-slate-900 dark:text-slate-100">
            {group.label}
          </span>
          <span className="mt-0.5 block font-mono text-xs text-slate-400">
            {group.agreement_type}
          </span>
        </span>
        <span className="flex shrink-0 flex-wrap items-center justify-end gap-2">
          <Badge text={`${active.length} active`} variant={active.length ? 'success' : 'neutral'} />
          {mandatory.length ? <Badge text={`${mandatory.length} mandatory`} variant="info" /> : null}
          {mapped.length - active.length ? (
            <Badge text={`${mapped.length - active.length} off`} variant="warning" />
          ) : null}
        </span>
      </button>

      {expanded ? (
        <div className="border-t border-slate-200 dark:border-slate-700">
          {mapped.length ? (
            <ul className="divide-y divide-slate-100 dark:divide-slate-700/50">
              {mapped.map((clause) => (
                <ClauseRow
                  key={clause.clause_key}
                  clause={clause}
                  editable={editable}
                  busy={busy}
                  onToggleActive={() =>
                    setMapping.mutate({
                      clause_key: clause.clause_key,
                      is_active: !clause.is_active,
                      is_mandatory: clause.is_mandatory,
                    })
                  }
                  onToggleMandatory={() =>
                    setMapping.mutate({
                      clause_key: clause.clause_key,
                      is_active: clause.is_active,
                      is_mandatory: !clause.is_mandatory,
                    })
                  }
                  onEdit={() => onEditClause(clause)}
                  onRemove={() => removeMapping.mutate(clause.clause_key)}
                />
              ))}
            </ul>
          ) : (
            <p className="px-5 py-4 text-sm text-slate-500">
              No clauses are configured for this agreement type, so a document of this type is
              checked against the whole Clause Master.
            </p>
          )}

          {editable && available.length ? (
            <AddClauseRow
              available={available}
              busy={busy}
              onAdd={(clauseKey) =>
                setMapping.mutate({ clause_key: clauseKey, is_active: true, is_mandatory: false })
              }
            />
          ) : null}
        </div>
      ) : null}
    </Card>
  );
}

function ClauseRow({
  clause,
  editable,
  busy,
  onToggleActive,
  onToggleMandatory,
  onEdit,
  onRemove,
}: {
  clause: AgreementClause;
  editable: boolean;
  busy: boolean;
  onToggleActive: () => void;
  onToggleMandatory: () => void;
  onEdit: () => void;
  onRemove: () => void;
}) {
  return (
    <li
      className={[
        'flex flex-col gap-3 px-5 py-3 sm:flex-row sm:items-center',
        clause.is_active ? '' : 'bg-slate-50/70 dark:bg-slate-900/30',
      ].join(' ')}
    >
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2">
          <span
            className={[
              'font-medium',
              clause.is_active
                ? 'text-slate-900 dark:text-slate-100'
                : 'text-slate-400 line-through',
            ].join(' ')}
          >
            {clause.name}
          </span>
          {clause.is_mandatory ? <Badge text="Mandatory" variant="info" /> : null}
          {clause.group_name ? (
            <span className="text-xs text-slate-400">{clause.group_name}</span>
          ) : null}
        </div>
        {clause.synonyms.length ? (
          <p className="mt-1 truncate text-xs text-slate-500">
            also called {clause.synonyms.join(', ')}
          </p>
        ) : null}
      </div>

      <div className="flex shrink-0 items-center gap-2">
        <Toggle
          label="Active"
          checked={clause.is_active}
          disabled={!editable || busy}
          onChange={onToggleActive}
        />
        <Toggle
          label="Mandatory"
          checked={clause.is_mandatory}
          disabled={!editable || busy || !clause.is_active}
          onChange={onToggleMandatory}
        />
        {editable ? (
          <>
            <button
              type="button"
              onClick={onEdit}
              title="Edit this clause everywhere it is used"
              aria-label={`Edit ${clause.name}`}
              className="rounded-lg p-1.5 text-slate-400 transition hover:bg-slate-100 hover:text-slate-700"
            >
              <Pencil className="h-3.5 w-3.5" />
            </button>
            <button
              type="button"
              onClick={onRemove}
              disabled={busy}
              title="Remove from this agreement type only"
              aria-label={`Remove ${clause.name} from this agreement type`}
              className="rounded-lg p-1.5 text-slate-400 transition hover:bg-rose-50 hover:text-rose-600 disabled:opacity-50"
            >
              <X className="h-3.5 w-3.5" />
            </button>
          </>
        ) : null}
      </div>
    </li>
  );
}

function Toggle({
  label,
  checked,
  disabled,
  onChange,
}: {
  label: string;
  checked: boolean;
  disabled: boolean;
  onChange: () => void;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      title={label}
      disabled={disabled}
      onClick={onChange}
      className={[
        'flex items-center gap-1.5 rounded-full px-2.5 py-1 text-[11px] font-medium ring-1 ring-inset transition',
        checked
          ? 'bg-blue-600 text-white ring-blue-600'
          : 'bg-white dark:bg-slate-800 text-slate-500 dark:text-slate-400 ring-slate-200 hover:bg-slate-50 dark:hover:bg-slate-700',
        disabled ? 'cursor-not-allowed opacity-50' : '',
      ].join(' ')}
    >
      <span className={['h-1.5 w-1.5 rounded-full', checked ? 'bg-white dark:bg-slate-800' : 'bg-slate-300'].join(' ')} />
      {label}
    </button>
  );
}

function AddClauseRow({
  available,
  busy,
  onAdd,
}: {
  available: AgreementClause[];
  busy: boolean;
  onAdd: (clauseKey: string) => void;
}) {
  const [open, setOpen] = useState(false);
  return (
    <div className="border-t border-slate-100 bg-slate-50/60 px-5 py-3 dark:border-slate-700/50 dark:bg-slate-900/30">
      {open ? (
        <div className="flex flex-wrap gap-2">
          {available.map((clause) => (
            <button
              key={clause.clause_key}
              type="button"
              disabled={busy}
              onClick={() => {
                onAdd(clause.clause_key);
                setOpen(false);
              }}
              className="rounded-full bg-white dark:bg-slate-800 px-3 py-1.5 text-xs font-medium text-slate-600 dark:text-slate-300 ring-1 ring-inset ring-slate-200 transition hover:bg-blue-50 hover:text-blue-700 disabled:opacity-50"
            >
              + {clause.name}
            </button>
          ))}
          <button
            type="button"
            onClick={() => setOpen(false)}
            className="px-2 text-xs font-medium text-slate-500 hover:text-slate-800"
          >
            Cancel
          </button>
        </div>
      ) : (
        <button
          type="button"
          onClick={() => setOpen(true)}
          className="text-xs font-medium text-blue-600 transition hover:text-blue-700"
        >
          + Add a clause to this agreement type ({available.length} available)
        </button>
      )}
    </div>
  );
}

// =============================================================================
// Create / edit a clause
// =============================================================================
function ClauseDialog({
  clause,
  onClose,
  onSaved,
}: {
  clause: AgreementClause | null;
  onClose: () => void;
  onSaved: () => void | Promise<unknown>;
}) {
  const isNew = clause === null;
  const [key, setKey] = useState('');
  const [name, setName] = useState(clause?.name ?? '');
  const [description, setDescription] = useState(clause?.description ?? '');
  const [groupName, setGroupName] = useState(clause?.group_name ?? '');
  const [synonyms, setSynonyms] = useState((clause?.synonyms ?? []).join(', '));
  const [error, setError] = useState<string | null>(null);

  const body = () => ({
    key: isNew ? key.trim() : undefined,
    name: name.trim(),
    description: description.trim() || null,
    group_name: groupName.trim() || null,
    synonyms: synonyms
      .split(',')
      .map((value) => value.trim())
      .filter(Boolean),
  });

  const save = useMutation({
    mutationFn: () =>
      isNew ? clauseApi.createClause(body()) : clauseApi.updateClause(clause.clause_key, body()),
    onSuccess: () => {
      setError(null);
      void onSaved();
    },
    onError: (caught) => setError(errorMessage(caught)),
  });

  const remove = useMutation({
    mutationFn: () => clauseApi.deleteClause(clause?.clause_key ?? ''),
    onSuccess: () => {
      setError(null);
      void onSaved();
    },
    onError: (caught) => setError(errorMessage(caught)),
  });

  return (
    <Modal
      open
      onClose={onClose}
      title={isNew ? 'New clause' : `Edit ${clause.name}`}
      description={
        isNew
          ? 'Added to the taxonomy. Attach it to an agreement type afterwards to start looking for it.'
          : 'Changes apply everywhere this clause is used, not only to the agreement type you opened it from.'
      }
      footer={
        <>
          {!isNew ? (
            <Button
              variant="ghost"
              icon={Trash2}
              busy={remove.isPending}
              onClick={() => remove.mutate()}
            >
              Retire
            </Button>
          ) : null}
          <Button variant="secondary" onClick={onClose}>
            Cancel
          </Button>
          <Button
            busy={save.isPending}
            disabled={!name.trim() || (isNew && !key.trim())}
            onClick={() => save.mutate()}
          >
            {isNew ? 'Create' : 'Save'}
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        {isNew ? (
          <label className="block space-y-1">
            <span className="text-sm font-medium text-slate-700">Key</span>
            <input
              value={key}
              onChange={(event) =>
                setKey(event.target.value.toLowerCase().replace(/[^a-z0-9_]/g, '_'))
              }
              placeholder="e.g. data_residency"
              className={`${inputClasses} font-mono`}
            />
            <span className="block text-xs text-slate-500">
              Permanent. Every extracted clause references it, so it cannot change afterwards.
            </span>
          </label>
        ) : null}

        <label className="block space-y-1">
          <span className="text-sm font-medium text-slate-700">Name</span>
          <input
            value={name}
            onChange={(event) => setName(event.target.value)}
            className={inputClasses}
          />
        </label>

        <label className="block space-y-1">
          <span className="text-sm font-medium text-slate-700">Alternative headings</span>
          <input
            value={synonyms}
            onChange={(event) => setSynonyms(event.target.value)}
            placeholder="Liability Cap, Limitation on Damages"
            className={inputClasses}
          />
          <span className="block text-xs text-slate-500">
            Comma separated. The strongest signal the detector has — a clause headed &quot;Liability
            Cap&quot; is found because this list says so.
          </span>
        </label>

        <label className="block space-y-1">
          <span className="text-sm font-medium text-slate-700">Group</span>
          <input
            value={groupName}
            onChange={(event) => setGroupName(event.target.value)}
            placeholder="Risk, Legal, Commercial..."
            className={inputClasses}
          />
        </label>

        <label className="block space-y-1">
          <span className="text-sm font-medium text-slate-700">Description</span>
          <textarea
            rows={2}
            value={description}
            onChange={(event) => setDescription(event.target.value)}
            className={`${inputClasses} resize-y`}
          />
        </label>

        {!isNew ? (
          <NoticeBanner message="Retiring removes this clause from every agreement type and from future extractions. Contracts already processed keep the clauses they were extracted with." />
        ) : null}

        {error ? <ErrorBanner message={error} /> : null}
      </div>
    </Modal>
  );
}

// =============================================================================
// Import
// =============================================================================
function ImportDialog({
  onClose,
  onImported,
}: {
  onClose: () => void;
  onImported: () => void | Promise<unknown>;
}) {
  const fileInput = useRef<HTMLInputElement>(null);
  const [fileName, setFileName] = useState<string | null>(null);
  const [parsed, setParsed] = useState<ParsedSheet | null>(null);
  const [deactivateMissing, setDeactivateMissing] = useState(false);
  const [result, setResult] = useState<ClauseImportResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function onPick(file: File | undefined) {
    if (!file) return;
    setError(null);
    setResult(null);
    try {
      const sheet = await parseClauseSheet(file);
      setParsed(sheet);
      setFileName(file.name);
    } catch (caught) {
      setParsed(null);
      setFileName(null);
      setError(errorMessage(caught));
    }
  }

  const apply = useMutation({
    mutationFn: () =>
      clauseApi.importRows(parsed?.rows ?? [], {
        deactivate_missing: deactivateMissing,
        create_missing_clauses: true,
      }),
    onSuccess: async (outcome) => {
      setResult(outcome);
      setError(null);
      await onImported();
    },
    onError: (caught) => setError(errorMessage(caught)),
  });

  const rowCount = parsed?.rows.length ?? 0;

  return (
    <Modal
      open
      onClose={onClose}
      title="Import clauses"
      description="Accepts .xlsx or .csv using the same columns the export produces."
      footer={
        result ? (
          <Button onClick={onClose}>Done</Button>
        ) : (
          <>
            <Button variant="secondary" onClick={onClose}>
              Cancel
            </Button>
            <Button busy={apply.isPending} disabled={!parsed} onClick={() => apply.mutate()}>
              {rowCount ? `Apply ${rowCount} rows` : 'Apply'}
            </Button>
          </>
        )
      }
    >
      <div className="space-y-3">
        {!result ? (
          <>
            <input
              ref={fileInput}
              type="file"
              accept=".csv,.xlsx,.xls"
              className="hidden"
              onChange={(event) => void onPick(event.target.files?.[0])}
            />
            <Button variant="secondary" icon={Upload} onClick={() => fileInput.current?.click()}>
              {fileName ?? 'Choose a file'}
            </Button>

            {parsed ? (
              <p className="text-sm text-slate-600">
                {rowCount} row{rowCount === 1 ? '' : 's'} read.
                {parsed.unknownColumns.length ? (
                  <span className="block text-xs text-amber-600">
                    Ignored unrecognised columns: {parsed.unknownColumns.join(', ')}
                  </span>
                ) : null}
              </p>
            ) : null}

            <label className="flex items-start gap-2 text-sm text-slate-700">
              <input
                type="checkbox"
                checked={deactivateMissing}
                onChange={(event) => setDeactivateMissing(event.target.checked)}
                className="mt-0.5"
              />
              <span>
                Deactivate clauses the file does not mention
                <span className="block text-xs text-slate-500">
                  Only within the agreement types the file covers. Off by default — a partial sheet
                  is the usual case, and switching off everything it omits is rarely what was meant.
                </span>
              </span>
            </label>
          </>
        ) : (
          <div className="space-y-2 text-sm">
            <p className="font-medium text-slate-800">
              {result.mappings_created} added, {result.mappings_updated} updated
              {result.mappings_deactivated ? `, ${result.mappings_deactivated} deactivated` : ''}.
            </p>
            {result.clauses_created || result.clauses_updated ? (
              <p className="text-slate-600">
                {result.clauses_created} new clause{result.clauses_created === 1 ? '' : 's'},{' '}
                {result.clauses_updated} edited.
              </p>
            ) : null}
            {result.skipped.length ? (
              <div className="rounded-xl bg-amber-50 p-3 text-xs text-amber-900">
                <p className="font-semibold">{result.skipped.length} row(s) skipped:</p>
                <ul className="mt-1 space-y-0.5">
                  {result.skipped.slice(0, 8).map((row, index) => (
                    <li key={`${row.row}-${index}`}>
                      row {row.row} ({row.clause_key || 'no key'}) — {row.reason}
                    </li>
                  ))}
                  {result.skipped.length > 8 ? <li>and {result.skipped.length - 8} more</li> : null}
                </ul>
              </div>
            ) : null}
          </div>
        )}

        {error ? <ErrorBanner message={error} /> : null}
      </div>
    </Modal>
  );
}

export default ClauseMasterPage;
