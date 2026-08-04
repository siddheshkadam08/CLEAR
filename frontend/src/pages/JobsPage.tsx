/**
 * Processing.
 *
 * The pipeline is six isolated stages, each emitting an artifact that doubles as a
 * checkpoint, so a failure is always attributable to one stage and a retry does not
 * repeat the ones that succeeded. This screen exposes that: which stage failed,
 * why, whether a retry can help, and reprocessing from any chosen stage.
 *
 * Health is here too, including stages that failed to import. A stage that is not
 * registered fails every job that reaches it, and without this it looks like an
 * unexplained per-contract failure rather than a deployment problem.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  Activity,
  AlertTriangle,
  ChevronDown,
  ChevronUp,
  FileArchive,
  Layers,
  ListChecks,
  RotateCcw,
  X,
  XCircle,
} from 'lucide-react';
import { useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';

import { jobs as jobsApi } from '@/api/endpoints';
import { errorMessage } from '@/api/errors';
import type { JobListItem, JobState } from '@/api/types';
import { Badge } from '@/components/common/Badge';
import { formatStatusLabel, getStatusVariant } from '@/lib/badges';
import { ErrorBanner } from '@/components/common/Banner';
import { Button } from '@/components/common/Button';
import { ACCENTS, Card, MetricCard, PageHeader, SectionHeader } from '@/components/common/Card';
import { EmptyState } from '@/components/common/EmptyState';
import { FilterChip } from '@/components/common/FilterChip';
import { selectClasses, SelectChevron } from '@/components/common/Field';
import { LoadingSpinner } from '@/components/common/LoadingSpinner';
import { formatDateTime, formatDateTimeFull, formatDuration, humanise } from '@/lib/format';
import { groupByArchive } from '@/lib/job-grouping';
import { useProjectScope } from '@/lib/scope';

/** The values the API accepts, exactly as `JobState` spells them.
 *
 * These were lowercase, and `JobState` is an uppercase `StrEnum` - so every chip
 * sent `state=ready`, FastAPI rejected it against the enum, and the request came
 * back 422. The list simply stopped filtering, and because the page still
 * rendered the unfiltered rows it looked like a filter with nothing to match
 * rather than a request that failed. Sent verbatim, humanised only for display.
 */
const STATES: JobState[] = ['QUEUED', 'READY', 'FAILED', 'RETRYING', 'CANCELLED', 'PAUSED'];

/** States from which a job will not move again. */
const TERMINAL_STATES: JobState[] = ['READY', 'FAILED', 'CANCELLED'];

/** The dispatched pipeline, in order. Mirrors STAGE_ORDER on the backend.
 *
 * This listed the original eight, four of which are retired and no longer
 * registered - so the "Stages registered" tile compared 6 against 8 and showed a
 * permanent alarm for a healthy deployment.
 */
const STAGES = [
  'validation',
  'parser',
  'docpipeline',
  'extraction',
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
      query.state.data?.items.some((job) => !TERMINAL_STATES.includes(job.state))
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
      <PageHeader
        title="Processing"
        subtitle="Six stages per contract. Each stage checkpoints, so a retry resumes rather than restarts."
      />

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
        <div className="grid grid-cols-2 gap-4 xl:grid-cols-4">
          <MetricCard
            label="In flight"
            value={health.in_flight}
            icon={Activity}
            accent={ACCENTS[0]}
            hint="running now"
          />
          <MetricCard
            label="Stalled"
            value={health.stalled}
            icon={AlertTriangle}
            accent={ACCENTS[3]}
            alarm={health.stalled > 0}
            hint="lease expired, awaiting reclaim"
          />
          <MetricCard
            label="Dead letter"
            value={health.dead_letter_count}
            icon={XCircle}
            accent={ACCENTS[8]}
            alarm={health.dead_letter_count > 0}
            hint="exhausted every retry"
          />
          <MetricCard
            label="Stages registered"
            value={`${health.registered_stages.length} / ${STAGES.length}`}
            icon={Layers}
            accent={ACCENTS[4]}
            alarm={health.registered_stages.length < STAGES.length}
            hint="worker has handlers for these"
          />
        </div>
      ) : null}

      {health && health.queues.length ? (
        <Card className="overflow-hidden">
          <SectionHeader
            title="Queues"
            subtitle="Depth per stage. Delayed rows are waiting out a retry backoff, not stuck."
            icon={ListChecks}
          />
          <div className="-mx-6 overflow-x-auto px-6">
            <table className="w-full min-w-[34rem] text-left text-sm">
              <thead className="border-b border-slate-200 dark:border-slate-700 text-xs uppercase tracking-wider text-slate-500 dark:text-slate-400">
                <tr>
                  <th scope="col" className="py-2 pr-4 font-semibold">Queue</th>
                  <th scope="col" className="py-2 pr-4 text-right font-semibold">Waiting</th>
                  <th scope="col" className="py-2 pr-4 text-right font-semibold">Active</th>
                  <th scope="col" className="py-2 pr-4 text-right font-semibold">Delayed</th>
                  <th scope="col" className="py-2 pr-4 text-right font-semibold">Failed</th>
                  <th scope="col" className="py-2 text-right font-semibold">Completed</th>
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
              <FilterChip
                key={state}
                // Lower-cased first: the value is an uppercase enum member, and
                // both `humanise` and `formatStatusLabel` only touch the first
                // letter - so "QUEUED" would stay shouting.
                label={formatStatusLabel(state.toLowerCase())}
                active={active}
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
              />
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
          {groupByArchive(jobsQuery.data.items).map((group) =>
            group.archive ? (
              <ArchiveGroup
                key={group.key}
                name={group.archive}
                jobs={group.jobs}
                focusJob={focusJob}
              />
            ) : (
              group.jobs.map((job) => (
                <JobCard key={job.id} job={job} defaultOpen={job.id === focusJob} />
              ))
            ),
          )}
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

// =============================================================================
// Job
// =============================================================================
// =============================================================================
// Archive grouping
// =============================================================================
function ArchiveGroup({
  name,
  jobs,
  focusJob,
}: {
  name: string;
  jobs: JobListItem[];
  focusJob?: string | null;
}) {
  const done = jobs.filter((job) => job.state === 'READY').length;
  const failed = jobs.filter((job) => job.state === 'FAILED').length;

  return (
    <section className="rounded-2xl border border-slate-200 dark:border-slate-700 bg-slate-50/60 dark:bg-slate-900/40 p-3 sm:p-4">
      <header className="mb-3 flex flex-wrap items-center gap-x-3 gap-y-1">
        <FileArchive className="h-4 w-4 shrink-0 text-slate-500" aria-hidden />
        <span className="font-medium text-slate-800">{name}</span>
        <span className="text-xs text-slate-500">
          {jobs.length} documents · {done} finished
          {failed ? ` · ${failed} failed` : ''}
        </span>
      </header>
      {/* Each document keeps its own card: its own state, progress, retry and
          logs. The group is presentation - it never merges their fates, and one
          failing here must not read as the archive failing. */}
      <div className="space-y-3">
        {jobs.map((job) => (
          <JobCard key={job.id} job={job} defaultOpen={job.id === focusJob} />
        ))}
      </div>
    </section>
  );
}

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

  const terminal = TERMINAL_STATES.includes(job.state);
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
          {job.contract_title || job.original_file_name ? (
            <p className="mt-1.5 truncate text-sm font-medium text-slate-900 dark:text-slate-100">
              {job.contract_title ?? job.original_file_name}
              {/* A Word upload processes a PDF, so without this the row gives no
                  sign the source was a .docx - and the user is left wondering
                  whether their document arrived at all. */}
              {job.original_file_type && job.original_file_type !== 'pdf' ? (
                <span className="ml-2 rounded bg-slate-100 px-1.5 py-0.5 text-[11px] font-normal uppercase tracking-wide text-slate-500">
                  from {job.original_file_type}
                </span>
              ) : null}
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
          {job.state === 'FAILED' ? (
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
          {job.finished_at ? <span>finished {formatDateTimeFull(job.finished_at)}</span> : null}
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
                <thead className="border-b border-slate-200 dark:border-slate-700 text-xs uppercase tracking-wider text-slate-500 dark:text-slate-400">
                  <tr>
                    <th scope="col" className="py-2 pr-4 font-semibold">Stage</th>
                    <th scope="col" className="py-2 pr-4 font-semibold">Status</th>
                    <th scope="col" className="py-2 pr-4 text-right font-semibold">Attempt</th>
                    <th scope="col" className="py-2 pr-4 text-right font-semibold">Duration</th>
                    <th scope="col" className="py-2 font-semibold">Notes</th>
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
