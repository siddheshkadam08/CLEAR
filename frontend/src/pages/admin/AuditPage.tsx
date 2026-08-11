/**
 * Activity log.
 *
 * The `audit_log` table has had write sites since the beginning - every login,
 * permission denial, clause review, export and configuration change - and until
 * now no way to read them. `AuditRepository.filtered` was written for this screen
 * and then never called, so a compliance trail the platform is careful to record
 * could only be reached with psql.
 *
 * Two decisions shape the layout:
 *
 * - **Failures are the reason anyone opens this.** A denied permission or a failed
 *   login is what an investigation looks for, so unsuccessful rows are marked in
 *   the list rather than buried in a detail pane, and there is a filter for them.
 * - **`before`/`after` are shown verbatim.** They are redacted on write - secrets
 *   never reach the row - so nothing is hidden at read time. A viewer that dropped
 *   fields would misrepresent what was recorded, which defeats the point of having
 *   a trail.
 */

import { keepPreviousData, useQuery } from '@tanstack/react-query';
import { ScrollText, ShieldAlert } from 'lucide-react';
import { useState } from 'react';
import { useSearchParams } from 'react-router-dom';

import { audit as auditApi } from '@/api/endpoints';
import { errorMessage } from '@/api/errors';
import type { AuditEntry } from '@/api/types';
import { Badge } from '@/components/common/Badge';
import { ErrorBanner } from '@/components/common/Banner';
import { Card, PageHeader } from '@/components/common/Card';
import { EmptyState } from '@/components/common/EmptyState';
import { inputClasses, selectClasses, SelectChevron } from '@/components/common/Field';
import { LoadingSpinner } from '@/components/common/LoadingSpinner';
import { Pagination } from '@/components/common/Pagination';
import { formatDateTimeFull, humanise } from '@/lib/format';
import { useProjectScope } from '@/lib/scope';

const PAGE_SIZE = 25;

/** The actions worth filtering to. Mirrors `AuditAction` on the backend. */
const ACTIONS = [
  'login',
  'logout',
  'create',
  'update',
  'delete',
  'review_decision',
  'export',
  'search',
  'config_change',
  'permission_denied',
];

export function AuditPage() {
  const { projectId } = useProjectScope();
  const [params, setParams] = useSearchParams();
  const [entityType, setEntityType] = useState(params.get('entity_type') ?? '');

  const page = Number(params.get('page') ?? 1);
  const action = params.get('action') ?? '';
  const outcome = params.get('outcome') ?? '';

  const query = useQuery({
    queryKey: ['audit', projectId, params.toString()],
    queryFn: () =>
      auditApi.list({
        page,
        size: PAGE_SIZE,
        project_id: projectId ?? undefined,
        action: action || undefined,
        entity_type: params.get('entity_type') || undefined,
        // Tri-state: unset means both, which is not the same as `false`.
        succeeded: outcome === '' ? undefined : outcome === 'ok',
      }),
    placeholderData: keepPreviousData,
  });

  function update(mutate: (next: URLSearchParams) => void) {
    const next = new URLSearchParams(params);
    mutate(next);
    next.delete('page');
    setParams(next, { replace: true });
  }

  const data = query.data;

  return (
    <div className="space-y-5">
      <PageHeader
        subtitle="Who did what, when. Scoped to the projects you can see, plus platform-level events."
      />

      <Card dense>
        <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
          <div className="relative sm:w-52">
            <select
              aria-label="Action"
              value={action}
              onChange={(event) =>
                update((next) => {
                  if (event.target.value) next.set('action', event.target.value);
                  else next.delete('action');
                })
              }
              className={selectClasses}
            >
              <option value="">All actions</option>
              {ACTIONS.map((value) => (
                <option key={value} value={value}>
                  {humanise(value)}
                </option>
              ))}
            </select>
            <SelectChevron />
          </div>

          <div className="relative sm:w-44">
            <select
              aria-label="Outcome"
              value={outcome}
              onChange={(event) =>
                update((next) => {
                  if (event.target.value) next.set('outcome', event.target.value);
                  else next.delete('outcome');
                })
              }
              className={selectClasses}
            >
              <option value="">Any outcome</option>
              <option value="ok">Succeeded</option>
              <option value="failed">Failed</option>
            </select>
            <SelectChevron />
          </div>

          <input
            type="search"
            placeholder="Entity type, e.g. contract"
            aria-label="Entity type"
            value={entityType}
            onChange={(event) => setEntityType(event.target.value)}
            onKeyDown={(event) => {
              if (event.key !== 'Enter') return;
              update((next) => {
                if (entityType.trim()) next.set('entity_type', entityType.trim());
                else next.delete('entity_type');
              });
            }}
            className={`${inputClasses} sm:flex-1`}
          />
        </div>
      </Card>

      {query.error ? (
        <ErrorBanner message={errorMessage(query.error)} onRetry={() => void query.refetch()} />
      ) : null}

      {query.isLoading ? (
        <Card>
          <LoadingSpinner label="Loading activity..." />
        </Card>
      ) : data?.items.length ? (
        <Card className="overflow-hidden p-0">
          <div className="divide-y divide-slate-100 dark:divide-slate-700/50">
            {data.items.map((entry) => (
              <AuditRow key={entry.id} entry={entry} />
            ))}
          </div>
          {data.meta.pages > 1 ? (
            <div className="border-t border-slate-200 bg-slate-50/80 px-5 py-3 dark:border-slate-700 dark:bg-slate-800/80">
              <Pagination
                page={data.meta.page}
                pages={data.meta.pages}
                total={data.meta.total}
                pageSize={PAGE_SIZE}
                onPage={(next) => {
                  const params2 = new URLSearchParams(params);
                  params2.set('page', String(next));
                  setParams(params2);
                }}
              />
            </div>
          ) : null}
        </Card>
      ) : (
        <EmptyState
          icon={ScrollText}
          title="No activity matches these filters"
          description="Every mutating action is recorded here. Widen the filters, or check a different project."
        />
      )}
    </div>
  );
}

function AuditRow({ entry }: { entry: AuditEntry }) {
  const [open, setOpen] = useState(false);
  const changed = entry.before || entry.after;

  return (
    <div className={entry.succeeded ? '' : 'bg-rose-50/40 dark:bg-rose-950/10'}>
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
        className="flex w-full items-start gap-3 px-5 py-3 text-left transition hover:bg-slate-50 dark:hover:bg-slate-800/60"
      >
        <span className="mt-0.5 shrink-0">
          {entry.succeeded ? (
            <Badge text={humanise(entry.action)} variant="neutral" />
          ) : (
            <Badge text={humanise(entry.action)} variant="danger" />
          )}
        </span>

        <span className="min-w-0 flex-1">
          <span className="block truncate text-sm text-slate-800 dark:text-slate-100">
            {entry.entity_label ?? humanise(entry.entity_type)}
            {entry.error_code ? (
              <span className="ml-2 font-mono text-xs text-rose-600">{entry.error_code}</span>
            ) : null}
          </span>
          <span className="mt-0.5 block truncate text-xs text-slate-500 dark:text-slate-400">
            {entry.user_email ?? 'system'}
            {entry.ip ? ` · ${entry.ip}` : ''}
            {entry.route ? ` · ${entry.route}` : ''}
          </span>
        </span>

        <span className="shrink-0 text-xs tabular-nums text-slate-500 dark:text-slate-400">
          {formatDateTimeFull(entry.created_at)}
        </span>
      </button>

      {open ? (
        <div className="space-y-3 px-5 pb-4 text-xs">
          {!entry.succeeded ? (
            <p className="flex items-center gap-1.5 font-medium text-rose-600">
              <ShieldAlert className="h-3.5 w-3.5" />
              This action did not succeed.
            </p>
          ) : null}

          <dl className="grid gap-x-6 gap-y-1 sm:grid-cols-2">
            <Detail label="Entity" value={`${entry.entity_type}${entry.entity_id ? ` · ${entry.entity_id}` : ''}`} />
            <Detail label="Project" value={entry.project_id ?? 'platform-level'} />
            {/* The join back to the application logs for this exact request. */}
            <Detail label="Request" value={entry.request_id} />
            <Detail label="Trace" value={entry.trace_id} />
          </dl>

          {changed ? (
            <div className="grid gap-3 sm:grid-cols-2">
              <Payload label="Before" value={entry.before} />
              <Payload label="After" value={entry.after} />
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function Detail({ label, value }: { label: string; value?: string | null }) {
  return (
    <div className="flex gap-2">
      <dt className="shrink-0 text-slate-400">{label}</dt>
      <dd className="min-w-0 truncate font-mono text-slate-600 dark:text-slate-300">
        {value || '—'}
      </dd>
    </div>
  );
}

function Payload({ label, value }: { label: string; value?: Record<string, unknown> | null }) {
  if (!value || Object.keys(value).length === 0) return null;
  return (
    <div>
      <p className="mb-1 font-semibold uppercase tracking-wider text-slate-400">{label}</p>
      <pre className="overflow-x-auto rounded-lg bg-slate-50 p-2 font-mono text-[11px] leading-5 text-slate-700 dark:bg-slate-800 dark:text-slate-300">
        {JSON.stringify(value, null, 2)}
      </pre>
    </div>
  );
}

export default AuditPage;
