/**
 * Export history.
 *
 * `GET /exports` has existed, and been wrapped in `endpoints.ts`, since the export
 * feature shipped - with nothing calling it. The consequence was not cosmetic:
 * `ExportButton` holds its job id in component state, so navigating away while a
 * workbook built discarded the only reference to it. The job finished, the file
 * landed in storage, and there was no route in the product that could reach it
 * again. It expired unread.
 *
 * So this screen is the durable record the button never was. Two things follow
 * from that:
 *
 * - **Failed and expired rows are shown, not filtered away.** "Where is my
 *   export?" is answered by "it failed at 14:02, here is why" or "the file was
 *   purged after 48 hours" - both useful. An empty list is not.
 * - **The retention clock is on every row.** A file with two hours left is a
 *   different thing from one with two days, and the only place that is knowable
 *   is here.
 *
 * Rows are this user's own. The server scopes to the requester rather than the
 * project, because an export is a copy of data taken by a named person, and
 * listing other people's copies would expose what they have been looking at.
 */

import { keepPreviousData, useQuery, useQueryClient } from '@tanstack/react-query';
import { Download, FileSpreadsheet, RefreshCw } from 'lucide-react';
import { useState } from 'react';
import { useSearchParams } from 'react-router-dom';

import { exports as exportsApi } from '@/api/endpoints';
import { errorMessage } from '@/api/errors';
import type { ExportJob, ExportStatus, UUID } from '@/api/types';
import { Badge } from '@/components/common/Badge';
import type { BadgeVariant } from '@/components/common/Badge';
import { ErrorBanner } from '@/components/common/Banner';
import { Button } from '@/components/common/Button';
import { Card, PageHeader } from '@/components/common/Card';
import { EmptyState } from '@/components/common/EmptyState';
import { FilterChip } from '@/components/common/FilterChip';
import { LoadingSpinner } from '@/components/common/LoadingSpinner';
import { Pagination } from '@/components/common/Pagination';
import { formatBytes, formatDateTime, formatNumber, humanise } from '@/lib/format';

const PAGE_SIZE = 20;
const STATUSES: ExportStatus[] = ['queued', 'running', 'completed', 'failed', 'expired'];

const STATUS_VARIANT: Record<ExportStatus, BadgeVariant> = {
  queued: 'info',
  running: 'info',
  completed: 'success',
  failed: 'danger',
  expired: 'neutral',
};

const ENTITY_LABELS: Record<string, string> = {
  contracts: 'Contracts',
  clauses: 'Clauses',
  obligations: 'Obligations',
  risks: 'Risks',
  key_dates: 'Key dates',
  entities: 'Parties',
};

export function ExportsPage() {
  const [params, setParams] = useSearchParams();
  const queryClient = useQueryClient();
  const [actionError, setActionError] = useState<string | null>(null);

  const page = Number(params.get('page') ?? 1);
  const statuses = params.getAll('status') as ExportStatus[];

  const query = useQuery({
    queryKey: ['exports', page, statuses.join(',')],
    queryFn: () => exportsApi.list({ page, size: PAGE_SIZE, status: statuses }),
    placeholderData: keepPreviousData,
    // Polls only while something is actually building, and stops the moment the
    // last job settles. A finished export never changes again.
    refetchInterval: (result) =>
      (result.state.data?.items ?? []).some(
        (job) => job.status === 'queued' || job.status === 'running',
      )
        ? 2000
        : false,
  });

  async function download(id: UUID) {
    try {
      const link = await exportsApi.download(id);
      setActionError(null);
      // A plain navigation rather than fetch-then-blob: the URL is pre-signed and
      // short-lived, and letting the browser handle it keeps a large workbook out
      // of the tab's memory.
      window.location.assign(link.url);
      // The server counts the download, so the row is stale the moment we leave.
      await queryClient.invalidateQueries({ queryKey: ['exports'] });
    } catch (caught) {
      setActionError(errorMessage(caught));
    }
  }

  const data = query.data;
  const building = (data?.items ?? []).filter(
    (job) => job.status === 'queued' || job.status === 'running',
  ).length;

  return (
    <div className="space-y-5">
      <PageHeader
        title="Exports"
        subtitle="Workbooks you have requested. Files are kept for a limited window, then purged."
        actions={
          <Button
            variant="secondary"
            size="sm"
            icon={RefreshCw}
            busy={query.isFetching}
            onClick={() => void query.refetch()}
          >
            Refresh
          </Button>
        }
      />

      <Card dense>
        <div className="flex flex-wrap items-center gap-2">
          {STATUSES.map((status) => {
            const active = statuses.includes(status);
            return (
              <FilterChip
                key={status}
                label={humanise(status)}
                active={active}
                onClick={() => {
                  const next = new URLSearchParams(params);
                  const current = next.getAll('status') as ExportStatus[];
                  next.delete('status');
                  for (const entry of current.includes(status)
                    ? current.filter((value) => value !== status)
                    : [...current, status]) {
                    next.append('status', entry);
                  }
                  next.delete('page');
                  setParams(next, { replace: true });
                }}
              />
            );
          })}
          {statuses.length ? (
            <button
              type="button"
              onClick={() => {
                const next = new URLSearchParams(params);
                next.delete('status');
                next.delete('page');
                setParams(next, { replace: true });
              }}
              className="text-xs font-medium text-blue-600 hover:text-blue-700"
            >
              Clear
            </button>
          ) : null}
          {building ? (
            <span className="ml-auto text-xs text-slate-500">
              {building} still building — this list refreshes itself.
            </span>
          ) : null}
        </div>
      </Card>

      {query.error ? (
        <ErrorBanner message={errorMessage(query.error)} onRetry={() => void query.refetch()} />
      ) : null}
      {actionError ? <ErrorBanner message={actionError} /> : null}

      {query.isLoading ? (
        <Card>
          <LoadingSpinner label="Loading exports..." />
        </Card>
      ) : data?.items.length ? (
        <>
          <div className="space-y-3">
            {data.items.map((job) => (
              <ExportRow key={job.id} job={job} onDownload={() => void download(job.id)} />
            ))}
          </div>
          {data.meta.pages > 1 ? (
            <Card dense>
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
            </Card>
          ) : null}
        </>
      ) : (
        <EmptyState
          icon={FileSpreadsheet}
          title={statuses.length ? 'No exports match these filters' : 'You have not exported anything yet'}
          description={
            statuses.length
              ? 'Clear the filters to see everything you have requested.'
              : 'Use Export on the Contracts screen to build a workbook of the view you are looking at. It will appear here while it builds, and stay here until the file is purged.'
          }
        />
      )}
    </div>
  );
}

function ExportRow({ job, onDownload }: { job: ExportJob; onDownload: () => void }) {
  const running = job.status === 'queued' || job.status === 'running';
  const retention = retentionLabel(job);
  const entities = (job.entities ?? []).map((entry) => ENTITY_LABELS[entry] ?? humanise(entry));

  return (
    <Card dense>
      <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="truncate font-semibold text-slate-900 dark:text-slate-100">
              {job.file_name ?? `${job.export_format.toUpperCase()} export`}
            </span>
            <Badge text={humanise(job.status)} variant={STATUS_VARIANT[job.status] ?? 'neutral'} />
            <Badge text={job.export_format.toUpperCase()} variant="neutral" />
          </div>

          {entities.length ? (
            <p className="mt-1.5 text-xs text-slate-500">{entities.join(' · ')}</p>
          ) : null}

          <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-slate-500">
            <span>Requested {formatDateTime(job.created_at)}</span>
            {job.finished_at ? <span>Finished {formatDateTime(job.finished_at)}</span> : null}
            {job.row_count != null ? <span>{formatNumber(job.row_count)} rows</span> : null}
            {job.file_size != null ? <span>{formatBytes(job.file_size)}</span> : null}
            {job.download_count > 0 ? (
              <span>
                Downloaded {job.download_count} {job.download_count === 1 ? 'time' : 'times'}
              </span>
            ) : null}
            {retention ? <span className={retention.urgent ? 'text-amber-600' : undefined}>{retention.text}</span> : null}
          </div>

          {running ? (
            <div className="mt-3 h-2 w-full max-w-sm overflow-hidden rounded-full bg-slate-100">
              <div
                className="h-full rounded-full bg-blue-600 transition-all"
                style={{ width: `${job.progress ?? 0}%` }}
              />
            </div>
          ) : null}

          {/* The reason it failed, not just that it did - otherwise the only
              recourse is to try again and hope. */}
          {job.status === 'failed' ? (
            <p className="mt-3 rounded-xl bg-rose-50 px-3 py-2 text-xs text-rose-700">
              {String(job.error?.message ?? 'The export failed.')}
            </p>
          ) : null}

          {job.status === 'expired' ? (
            <p className="mt-3 text-xs text-slate-500">
              The file has been purged. This row is kept as the record that the export was
              taken; request a new one to get the data again.
            </p>
          ) : null}
        </div>

        <div className="flex shrink-0 items-center gap-2">
          {job.status === 'completed' ? (
            <Button size="sm" icon={Download} onClick={onDownload}>
              Download
            </Button>
          ) : running ? (
            <span className="text-xs tabular-nums text-slate-500">{job.progress ?? 0}%</span>
          ) : null}
        </div>
      </div>
    </Card>
  );
}

/** How long the file has left, or that it has already gone. */
function retentionLabel(job: ExportJob): { text: string; urgent: boolean } | null {
  if (job.status !== 'completed' || !job.expires_at) return null;
  const remaining = new Date(job.expires_at).getTime() - Date.now();
  if (remaining <= 0) return { text: 'Expiring now', urgent: true };
  const hours = Math.floor(remaining / 3_600_000);
  if (hours < 1) return { text: `Expires in ${Math.max(1, Math.round(remaining / 60_000))} min`, urgent: true };
  if (hours < 24) return { text: `Expires in ${hours}h`, urgent: hours <= 6 };
  return { text: `Expires in ${Math.floor(hours / 24)}d`, urgent: false };
}

export default ExportsPage;
