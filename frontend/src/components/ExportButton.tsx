/**
 * Export the current view.
 *
 * The filters passed in are the ones driving the table the user is looking at, so
 * the workbook is that view rather than an approximation of it. An export that
 * quietly returns a different set than the screen showed is worse than no export:
 * the file gets circulated and nobody re-checks it.
 *
 * The job is polled rather than awaited. Exports run outside the request, so the
 * button reports progress and then hands over a download link; it never blocks on
 * a spreadsheet that might take a minute to build.
 *
 * That polling lives in component state, so closing this modal or leaving the page
 * drops the reference. The job itself is unaffected - it finishes and the file
 * lands in storage - so the modal points at `/exports`, which is the durable
 * record. Before that screen existed, walking away mid-export lost the file for
 * good: it built, sat unreachable, and was purged at the end of its retention
 * window.
 *
 * The options render in a Modal rather than a floating popover: a 320px panel
 * anchored to a toolbar button falls off the side of a phone screen.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Download, FileSpreadsheet } from 'lucide-react';
import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';

import { exports as exportsApi } from '@/api/endpoints';
import { errorMessage } from '@/api/errors';
import type { ExportEntity, UUID } from '@/api/types';
import { ErrorBanner } from '@/components/common/Banner';
import { Button } from '@/components/common/Button';
import { FilterChip } from '@/components/common/FilterChip';
import { LoadingSpinner } from '@/components/common/LoadingSpinner';
import { Modal } from '@/components/common/Modal';
import { formatBytes } from '@/lib/format';

const ENTITY_LABELS: Record<ExportEntity, string> = {
  contracts: 'Contracts',
  clauses: 'Clauses',
  obligations: 'Obligations',
  risks: 'Risks',
  key_dates: 'Key dates',
  entities: 'Parties',
};

export interface ExportButtonProps {
  /** Filters in the same shape the contract list endpoint accepts. */
  filters: Record<string, unknown>;
  projectId: UUID | null;
  label?: string;
}

export function ExportButton({ filters, projectId, label = 'Export' }: ExportButtonProps) {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [entities, setEntities] = useState<ExportEntity[] | null>(null);
  const [jobId, setJobId] = useState<UUID | null>(null);
  const [error, setError] = useState<string | null>(null);

  const capabilities = useQuery({
    queryKey: ['export-capabilities'],
    queryFn: () => exportsApi.capabilities(),
    staleTime: 60 * 60_000,
    enabled: open,
  });

  const job = useQuery({
    queryKey: ['export', jobId],
    queryFn: () => exportsApi.get(jobId as UUID),
    enabled: Boolean(jobId),
    // Stop polling the moment it settles; a finished export never changes again.
    refetchInterval: (query) => {
      const state = query.state.data?.status;
      return state === 'queued' || state === 'running' ? 1500 : false;
    },
  });

  // The default entity set comes from the server, not from a constant here: which
  // entities exist is a backend concern, and hardcoding them would let the two
  // drift apart silently.
  useEffect(() => {
    if (entities === null && capabilities.data) {
      setEntities(capabilities.data.default_entities);
    }
  }, [capabilities.data, entities]);

  const create = useMutation({
    mutationFn: () =>
      exportsApi.create({
        export_format: 'xlsx',
        scope: projectId ? 'project' : 'application',
        project_id: projectId,
        entities: entities ?? undefined,
        filters,
      }),
    onSuccess: async (created) => {
      setJobId(created.id);
      setError(null);
      await queryClient.invalidateQueries({ queryKey: ['exports'] });
    },
    onError: (caught) => setError(errorMessage(caught)),
  });

  async function download() {
    if (!jobId) return;
    try {
      const link = await exportsApi.download(jobId);
      // A plain navigation rather than fetch-then-blob: the URL is pre-signed and
      // short-lived, and letting the browser handle it keeps a 200 MB workbook out
      // of the tab's memory.
      window.location.assign(link.url);
    } catch (caught) {
      setError(errorMessage(caught));
    }
  }

  function toggleEntity(entity: ExportEntity) {
    setEntities((current) => {
      const list = current ?? [];
      return list.includes(entity)
        ? list.filter((value) => value !== entity)
        : [...list, entity];
    });
  }

  const current = job.data;
  const running = current?.status === 'queued' || current?.status === 'running';

  return (
    <>
      <Button variant="secondary" size="sm" icon={Download} onClick={() => setOpen(true)}>
        {label}
      </Button>

      <Modal
        open={open}
        onClose={() => setOpen(false)}
        title="Export this view"
        description={
          Object.keys(filters).length
            ? 'Uses the filters currently applied to the list.'
            : 'No filters applied — every contract in scope.'
        }
        footer={
          current?.status === 'completed' ? (
            <>
              <Button
                variant="secondary"
                onClick={() => {
                  setJobId(null);
                  setError(null);
                }}
              >
                New export
              </Button>
              <Button icon={Download} onClick={() => void download()}>
                Download
                {current.file_size ? ` (${formatBytes(current.file_size)})` : ''}
              </Button>
            </>
          ) : (
            <>
              <Button variant="secondary" onClick={() => setOpen(false)}>
                Cancel
              </Button>
              <Button
                icon={FileSpreadsheet}
                busy={create.isPending || running}
                disabled={!entities?.length}
                onClick={() => create.mutate()}
              >
                {running
                  ? `Building… ${current?.progress ?? 0}%`
                  : create.isPending
                    ? 'Requesting…'
                    : 'Create export'}
              </Button>
            </>
          )
        }
      >
        <div className="space-y-4">
          {capabilities.isLoading ? (
            <LoadingSpinner size="sm" label="Loading export options..." />
          ) : (
            <div>
              <p className="mb-2 text-xs font-semibold uppercase tracking-wider text-slate-400">
                Include
              </p>
              <div className="flex flex-wrap gap-2">
                {(capabilities.data?.entities ?? []).map((entity) => {
                  const on = Boolean(entities?.includes(entity));
                  return (
                    <FilterChip
                      key={entity}
                      label={ENTITY_LABELS[entity] ?? entity}
                      active={on}
                      onClick={() => toggleEntity(entity)}
                    />
                  );
                })}
              </div>
              <p className="mt-2 text-xs text-slate-500">
                One worksheet each. Files are kept for{' '}
                {capabilities.data?.retention_hours ?? 48} hours.
              </p>
            </div>
          )}

          {running ? (
            <div className="h-2 w-full overflow-hidden rounded-full bg-slate-100">
              <div
                className="h-full rounded-full bg-blue-600 transition-all"
                style={{ width: `${current?.progress ?? 0}%` }}
              />
            </div>
          ) : null}

          {error ? <ErrorBanner message={error} /> : null}

          {current?.status === 'failed' ? (
            <ErrorBanner message={String(current.error?.message ?? 'The export failed.')} />
          ) : null}

          {current?.status === 'completed' && current.row_count !== null ? (
            <p className="text-xs text-slate-500">
              {current.row_count?.toLocaleString()} rows.
            </p>
          ) : null}

          {/* Shown once a job exists, including while it builds: this is exactly
              when someone is tempted to close the dialog and carry on. */}
          {current ? (
            <p className="text-xs text-slate-500">
              You can close this — it keeps building.{' '}
              <Link
                to="/exports"
                onClick={() => setOpen(false)}
                className="font-medium text-blue-600 hover:text-blue-700"
              >
                All your exports
              </Link>{' '}
              lists every workbook you have requested, with its download link.
            </p>
          ) : null}
        </div>
      </Modal>
    </>
  );
}

export default ExportButton;
