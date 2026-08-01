/**
 * Processing.
 *
 * The pipeline is eight isolated stages, each emitting an artifact that doubles as
 * a checkpoint, so a failure is always attributable to one stage and a retry does
 * not repeat the seven that succeeded. This screen exposes that: which stage failed,
 * why, whether a retry can help, and reprocessing from any chosen stage.
 *
 * Health is here too, including stages that failed to import. A stage that is not
 * registered fails every job that reaches it, and without this it looks like an
 * unexplained per-contract failure rather than a deployment problem.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ChevronDown, ChevronUp, ListChecks, RotateCcw, X } from 'lucide-react';
import { useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';

import { jobs as jobsApi } from '@/api/endpoints';
import { errorMessage } from '@/api/errors';
import type { JobListItem } from '@/api/types';
import { Badge } from '@/components/common/Badge';
import { formatStatusLabel, getStatusVariant } from '@/lib/badges';
import { ErrorBanner } from '@/components/common/Banner';
import { Button } from '@/components/common/Button';
import { Card, SectionHeader } from '@/components/common/Card';
import { EmptyState } from '@/components/common/EmptyState';
import { selectClasses, SelectChevron } from '@/components/common/Field';
import { LoadingSpinner } from '@/components/common/LoadingSpinner';
import { formatDateTime, formatDuration, humanise } from '@/lib/format';
import { useProjectScope } from '@/lib/scope';

const STATES = ['queued', 'ready', 'failed', 'retrying', 'cancelled', 'paused'];

const STAGES = [
  'validation',
  'parser',
  'enrichment',
  'classification',
  'chunking',
  'ai_extraction',
  'embedding',
  'indexing',
];

export function JobsPage() {
  const { projectId } = useProjectScope();
  const [params, setParams] = useSearchParams();
  const selectedStates = params.getAll('state');

  const jobsQuery = useQuery({
    queryKey: ['jobs', projectId, selectedStates.join(',')],
    queryFn: () =>
      jobsApi.list({
        project_id: projectId ?? undefined,
        state: selectedStates.length ? selectedStates : undefined,
        size: 50,
      }),
    refetchInterval: (query) =>
      query.state.data?.items.some(
        (job) => !['ready', 'failed', 'cancelled'].includes(job.state),
      )
        ? 4000
        : 15000,
  });

  const healthQuery = useQuery({
    queryKey: ['pipeline-health'],
    queryFn: () => jobsApi.health(),
    refetchInterval: 15000,
  });

  const health = healthQuery.data;
  const focusJob = params.get('job');

  return (
    <div className="space-y-5">
      {/* <PageHeader
        title="Processing"
        subtitle="Eight stages per contract. Each stage checkpoints, so a retry resumes rather than restarts."
      /> */}

      {health && health.unavailable_stages.length ? (
        <div className="rounded-2xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700">
          <p className="font-semibold">
            {health.unavailable_stages.length} stage
            {health.unavailable_stages.length === 1 ? '' : 's'} failed to load.
          </p>
          <p className="mt-1">
            Every job reaching {health.unavailable_stages.map(humanise).join(', ')} will fail
            until this is fixed. This is a deployment problem, not a document problem.
          </p>
          {Object.entries(health.import_errors).map(([stage, message]) => (
            <p key={stage} className="mt-1 break-all font-mono text-xs">
              {stage}: {message}
            </p>
          ))}
        </div>
      ) : null}

      {health ? (
        <div className="grid grid-cols-2 gap-3 sm:gap-4 xl:grid-cols-4">
          <HealthTile label="In flight" value={health.in_flight} />
          <HealthTile label="Stalled" value={health.stalled} alarm={health.stalled > 0} />
          <HealthTile
            label="Dead letter"
            value={health.dead_letter_count}
            alarm={health.dead_letter_count > 0}
          />
          <HealthTile
            label="Stages registered"
            value={health.registered_stages.length}
            alarm={health.registered_stages.length < STAGES.length}
          />
        </div>
      ) : null}

      {health && health.queues.length ? (
        <Card className="overflow-hidden">
          <SectionHeader
            title="Queues"
            subtitle="Depth per queue, straight from the broker."
            icon={ListChecks}
          />
          <div className="-mx-6 overflow-x-auto px-6">
            <table className="w-full min-w-[34rem] text-left text-sm">
              <thead className="border-b border-slate-200 text-xs uppercase tracking-wider text-slate-500">
                <tr>
                  <th className="py-2 pr-4 font-semibold">Queue</th>
                  <th className="py-2 pr-4 text-right font-semibold">Waiting</th>
                  <th className="py-2 pr-4 text-right font-semibold">Active</th>
                  <th className="py-2 pr-4 text-right font-semibold">Delayed</th>
                  <th className="py-2 pr-4 text-right font-semibold">Failed</th>
                  <th className="py-2 text-right font-semibold">Completed</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {health.queues.map((queue) => (
                  <tr key={queue.queue}>
                    <td className="py-2 pr-4 font-mono text-xs text-slate-700">
                      {queue.queue}
                    </td>
                    <td className="py-2 pr-4 text-right tabular-nums">{queue.waiting}</td>
                    <td className="py-2 pr-4 text-right tabular-nums">{queue.active}</td>
                    <td className="py-2 pr-4 text-right tabular-nums">{queue.delayed}</td>
                    <td
                      className={`py-2 pr-4 text-right tabular-nums ${queue.failed ? 'font-semibold text-rose-600' : ''}`}
                    >
                      {queue.failed}
                    </td>
                    <td className="py-2 text-right tabular-nums text-slate-500">
                      {queue.completed}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      ) : null}

      <Card dense>
        <div className="flex flex-wrap gap-2">
          {STATES.map((state) => {
            const active = selectedStates.includes(state);
            return (
              <button
                key={state}
                type="button"
                aria-pressed={active}
                onClick={() => {
                  const next = new URLSearchParams(params);
                  const current = next.getAll('state');
                  next.delete('state');
                  for (const existing of current) {
                    if (existing !== state) next.append('state', existing);
                  }
                  if (!current.includes(state)) next.append('state', state);
                  setParams(next, { replace: true });
                }}
                className={[
                  'rounded-full px-3 py-1.5 text-xs font-medium ring-1 ring-inset transition',
                  active
                    ? 'bg-blue-600 text-white ring-blue-600'
                    : 'bg-white text-slate-600 ring-slate-200 hover:bg-slate-50',
                ].join(' ')}
              >
                {humanise(state)}
              </button>
            );
          })}
        </div>
      </Card>

      {jobsQuery.error ? (
        <ErrorBanner
          message={errorMessage(jobsQuery.error)}
          onRetry={() => void jobsQuery.refetch()}
        />
      ) : null}

      {jobsQuery.isLoading ? (
        <Card>
          <LoadingSpinner label="Loading processing runs..." />
        </Card>
      ) : jobsQuery.data?.items.length ? (
        <div className="space-y-3">
          {jobsQuery.data.items.map((job) => (
            <JobCard key={job.id} job={job} defaultOpen={job.id === focusJob} />
          ))}
        </div>
      ) : (
        <EmptyState
          icon={ListChecks}
          title="No processing runs"
          description="Runs appear here as soon as a contract is uploaded, and update live as each stage completes."
        />
      )}
    </div>
  );
}

function HealthTile({
  label,
  value,
  alarm = false,
}: {
  label: string;
  value: number;
  alarm?: boolean;
}) {
  return (
    <div className="rounded-2xl border border-slate-200 bg-white p-4 shadow-sm sm:p-5">
      <p className="truncate text-xs font-medium text-slate-500 sm:text-sm">{label}</p>
      <p
        className={[
          'mt-2 text-2xl font-semibold tabular-nums sm:text-3xl',
          alarm ? 'text-rose-600' : 'text-slate-900',
        ].join(' ')}
      >
        {value}
      </p>
    </div>
  );
}

// =============================================================================
// Job
// =============================================================================
function JobCard({ job, defaultOpen }: { job: JobListItem; defaultOpen?: boolean }) {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(Boolean(defaultOpen));
  const [fromStage, setFromStage] = useState('ai_extraction');
  const [actionError, setActionError] = useState<string | null>(null);

  // Stage runs, the structured error and retryability are on the detail response
  // only. Fetching them for every row of the list would mean a stage join per row
  // to render a table nobody has expanded yet - so they load when the row opens.
  const detailQuery = useQuery({
    queryKey: ['job', job.id],
    queryFn: () => jobsApi.get(job.id),
    enabled: open,
  });
  const detail = detailQuery.data;

  const invalidate = async () => {
    await queryClient.invalidateQueries({ queryKey: ['jobs'] });
    await queryClient.invalidateQueries({ queryKey: ['job', job.id] });
    await queryClient.invalidateQueries({ queryKey: ['contract-jobs', job.contract_id] });
  };

  const retry = useMutation({
    mutationFn: () => jobsApi.retry(job.id),
    onSuccess: invalidate,
    onError: (caught) => setActionError(errorMessage(caught)),
  });

  const cancel = useMutation({
    mutationFn: () => jobsApi.cancel(job.id),
    onSuccess: invalidate,
    onError: (caught) => setActionError(errorMessage(caught)),
  });

  const reprocess = useMutation({
    mutationFn: () => jobsApi.reprocess(job.contract_id, { from_stage: fromStage }),
    onSuccess: invalidate,
    onError: (caught) => setActionError(errorMessage(caught)),
  });

  const terminal = ['ready', 'failed', 'cancelled'].includes(job.state);
  const failedStage = detail?.stages.find((stage) => stage.status === 'failed');
  // Until the detail has loaded, retryability is unknown. Offering the button
  // optimistically would produce a request the server rejects; withholding it
  // entirely would hide a valid action. Show it, and let the row settle.
  const retryable = detail ? detail.is_retryable : undefined;

  return (
    <Card dense>
      <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <Badge text={formatStatusLabel(job.state)} variant={getStatusVariant(job.state)} />
            {job.retry_count > 0 ? (
              <Badge
                text={`Retry ${job.retry_count}${detail ? `/${detail.max_retries}` : ''}`}
                variant="neutral"
              />
            ) : null}
            <span className="font-mono text-xs text-slate-400">{job.id.slice(0, 8)}</span>
          </div>
          {job.contract_title ? (
            <p className="mt-1.5 truncate text-sm font-medium text-slate-900">
              {job.contract_title}
            </p>
          ) : null}
          <Link
            to={`/contracts/${job.contract_id}`}
            className="mt-0.5 inline-block text-xs font-medium text-blue-600 hover:text-blue-700"
          >
            Open contract
          </Link>
        </div>

        <div className="flex flex-wrap gap-2 lg:shrink-0">
          {job.state === 'failed' ? (
            // Offered only when the backend says a retry could plausibly help.
            // Re-running a deterministic validation failure just burns a queue slot
            // and tells the user nothing new.
            retryable !== false ? (
              <Button
                size="sm"
                icon={RotateCcw}
                busy={retry.isPending}
                disabled={retryable === undefined}
                onClick={() => retry.mutate()}
              >
                Retry
              </Button>
            ) : (
              <Badge text="Not retryable" variant="neutral" />
            )
          ) : null}
          {!terminal ? (
            <Button
              variant="secondary"
              size="sm"
              icon={X}
              busy={cancel.isPending}
              onClick={() => cancel.mutate()}
            >
              Cancel
            </Button>
          ) : null}
          <Button
            variant="secondary"
            size="sm"
            icon={open ? ChevronUp : ChevronDown}
            onClick={() => setOpen((value) => !value)}
          >
            {open ? 'Hide stages' : 'Stages'}
          </Button>
        </div>
      </div>

      <div className="mt-4">
        <div className="h-2 w-full overflow-hidden rounded-full bg-slate-100">
          <div
            className="h-full rounded-full bg-blue-600 transition-all"
            style={{ width: `${job.progress}%` }}
          />
        </div>
        <div className="mt-2 flex flex-col gap-1 text-xs text-slate-500 sm:flex-row sm:justify-between">
          <span>
            {job.current_stage ? humanise(job.current_stage) : humanise(job.state)} Â· started{' '}
            {formatDateTime(job.created_at)}
          </span>
          {job.finished_at ? <span>finished {formatDateTime(job.finished_at)}</span> : null}
        </div>
      </div>

      {job.error_message || detail?.error ? (
        <div className="mt-3 rounded-2xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700">
          <p className="font-semibold">
            {failedStage ? `${humanise(failedStage.stage)} failed` : 'Processing failed'}
          </p>
          <p className="mt-1">
            {String(
              detail?.error?.message ??
                detail?.error?.detail ??
                detail?.error?.code ??
                job.error_message ??
                'Unknown error',
            )}
          </p>
          {detail?.error?.trace_id !== undefined ? (
            <p className="mt-1 font-mono text-xs">Trace {String(detail.error.trace_id)}</p>
          ) : null}
        </div>
      ) : null}

      {actionError ? (
        <div className="mt-3">
          <ErrorBanner message={actionError} />
        </div>
      ) : null}

      {open ? (
        <div className="mt-4 border-t border-slate-100 pt-4">
          {detailQuery.isLoading ? (
            <LoadingSpinner size="sm" label="Loading stages..." />
          ) : detailQuery.error ? (
            <ErrorBanner
              message={errorMessage(detailQuery.error)}
              onRetry={() => void detailQuery.refetch()}
            />
          ) : (
            <div className="-mx-5 overflow-x-auto px-5">
              <table className="w-full min-w-[32rem] text-left text-sm">
                <thead className="border-b border-slate-200 text-xs uppercase tracking-wider text-slate-500">
                  <tr>
                    <th className="py-2 pr-4 font-semibold">Stage</th>
                    <th className="py-2 pr-4 font-semibold">Status</th>
                    <th className="py-2 pr-4 text-right font-semibold">Attempt</th>
                    <th className="py-2 pr-4 text-right font-semibold">Duration</th>
                    <th className="py-2 font-semibold">Notes</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {(detail?.stages ?? []).map((stage) => (
                    <tr key={stage.id}>
                      <td className="py-2 pr-4 text-slate-700">{humanise(stage.stage)}</td>
                      <td className="py-2 pr-4">
                        <div className="flex flex-wrap items-center gap-1.5">
                          <Badge
                            text={formatStatusLabel(stage.status)}
                            variant={getStatusVariant(stage.status)}
                          />
                          {stage.reused_checkpoint ? (
                            <Badge
                              text="Cached"
                              variant="neutral"
                              title="Reused a valid artifact from a previous run"
                            />
                          ) : null}
                        </div>
                      </td>
                      <td className="py-2 pr-4 text-right tabular-nums text-slate-600">
                        {stage.attempt}
                      </td>
                      <td className="py-2 pr-4 text-right tabular-nums text-slate-600">
                        {formatDuration(stage.duration_ms)}
                      </td>
                      <td className="py-2 text-xs text-slate-500">
                        {stage.warnings.length ? stage.warnings.join('; ') : 'â€”'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {/* Reprocessing from a stage rather than from the start: re-parsing a
              300-page contract to re-run extraction wastes the most expensive
              stage in the pipeline to redo one of the cheaper ones. */}
          <div className="mt-4 flex flex-col gap-2 sm:flex-row sm:items-center">
            <span className="text-sm text-slate-500">Reprocess from</span>
            <div className="relative sm:w-48">
            <select
              value={fromStage}
              onChange={(event) => setFromStage(event.target.value)}
              aria-label="Reprocess from stage"
              className={selectClasses}
            >
              {STAGES.map((stage) => (
                <option key={stage} value={stage}>
                  {humanise(stage)}
                </option>
              ))}
            </select>
            <SelectChevron />
            </div>
            <Button
              variant="secondary"
              size="sm"
              busy={reprocess.isPending}
              onClick={() => reprocess.mutate()}
            >
              {reprocess.isPending ? 'Queueingâ€¦' : 'Reprocess'}
            </Button>
            <span className="text-xs text-slate-500">
              Earlier stages reuse their checkpoints.
            </span>
          </div>
        </div>
      ) : null}
    </Card>
  );
}

export default JobsPage;
