/**
 * Contract upload.
 *
 * A contract belongs to exactly one project and the project is the security
 * boundary, so the destination is chosen explicitly rather than inherited from
 * whatever happens to be selected in the header - putting a document in the wrong
 * project is a disclosure, not a tidiness problem.
 *
 * Administrators never reach this screen (see `MemberRoute` in App.tsx, and
 * `ADMIN_EXCLUDED_PERMISSIONS` on the server, which is what actually enforces it).
 */

import { useQueryClient } from '@tanstack/react-query';
import {
  CheckCircle2,
  CloudUpload,
  Copy,
  FileText,
  FolderOpen,
  Trash2,
  UploadCloud,
  XCircle,
} from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';

import { contracts as contractsApi } from '@/api/endpoints';
import { errorMessage } from '@/api/errors';
import type { UUID } from '@/api/types';
import { ErrorBanner } from '@/components/common/Banner';
import { Button } from '@/components/common/Button';
import { Card, PageHeader, SectionHeader } from '@/components/common/Card';
import { EmptyState } from '@/components/common/EmptyState';
import { Field, selectClasses, SelectChevron } from '@/components/common/Field';
import { formatBytes } from '@/lib/format';
import { useProjectScope } from '@/lib/scope';

/** `expanded` is a ZIP that became several contracts rather than one. */
type ItemState = 'pending' | 'uploading' | 'done' | 'duplicate' | 'error' | 'expanded';

interface UploadItem {
  id: string;
  file: File;
  state: ItemState;
  contractId?: UUID;
  /** The contract this file duplicates, so the user can go and look at it. */
  existingContractId?: UUID;
  message?: string;
}

/**
 * What the backend accepts. Kept in step with `ALLOWED_FILE_TYPES`.
 *
 * Browsers are inconsistent about the MIME type they report for these - a .doc
 * can arrive as `application/msword`, `application/octet-stream`, or an empty
 * string depending on the OS and how the file was created - so the check below
 * treats an unrecognised-but-present type as "let the server decide" rather than
 * rejecting client-side. The server sniffs magic bytes and is the authority.
 */
const ACCEPTED = [
  'application/pdf',
  'application/msword',
  'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  'application/zip',
  'application/x-zip-compressed',
  'application/octet-stream',
];
const ACCEPT_ATTR = '.pdf,.doc,.docx,.zip';
/** Extensions are the reliable signal; MIME type is advisory. */
const ACCEPTED_EXTENSIONS = ['pdf', 'doc', 'docx', 'zip'];
/**
 * The smallest ceiling in the chain, which is the only one a user experiences.
 *
 * Three limits disagreed: this said 100MB, `MAX_UPLOAD_SIZE_MB` said 200, and the
 * layout extractor reports `maxUploadMB: 50`. A 60MB scan therefore passed both of
 * our checks, was stored, queued, and only failed once the parser rejected it - by
 * which point the user had watched a progress bar run to completion. The limit
 * that stops an upload should be the one that will actually stop it.
 */
const MAX_BYTES = 50 * 1024 * 1024;

export function UploadPage() {
  const { projects, projectId: scopedProject } = useProjectScope();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const inputRef = useRef<HTMLInputElement>(null);

  const [target, setTarget] = useState<UUID | ''>(scopedProject ?? '');
  const [items, setItems] = useState<UploadItem[]>([]);
  const [dragging, setDragging] = useState(false);
  const [progress, setProgress] = useState(0);
  const [busy, setBusy] = useState(false);
  const [batchError, setBatchError] = useState<string | null>(null);

  // Follow the header.
  //
  // `useState` above seeds the target once, on the first render - so changing the
  // business unit afterwards left it pointing at whatever was selected when the
  // page mounted. The header said one unit and the upload posted to another, which
  // is the one bug on this screen that *writes* rather than merely showing the
  // wrong thing: the file lands in the wrong business unit and looks fine.
  //
  // Only follows the header, never the in-page selector: picking a different
  // destination below is a deliberate choice for this batch, and clobbering it on
  // re-render would make that control unusable.
  useEffect(() => {
    if (scopedProject) setTarget(scopedProject);
  }, [scopedProject]);

  function addFiles(files: FileList | File[]) {
    const next: UploadItem[] = [];
    for (const file of Array.from(files)) {
      const tooBig = file.size > MAX_BYTES;
      // Extension first: it is what the user controls and what the server keys
      // off. The MIME check only rejects a type we positively recognise as
      // something else, so a .docx reported as `application/octet-stream` - which
      // happens routinely - still reaches the server.
      const extension = file.name.includes('.')
        ? file.name.split('.').pop()!.toLowerCase()
        : '';
      const wrongType =
        !ACCEPTED_EXTENSIONS.includes(extension) || (file.type ? !ACCEPTED.includes(file.type) : false);
      next.push({
        // Name plus size plus index: two files can share a name, and React needs
        // a stable key that survives the list being rewritten mid-upload.
        id: `${file.name}:${file.size}:${next.length}:${items.length}`,
        file,
        state: tooBig || wrongType ? 'error' : 'pending',
        message: tooBig
          ? `Too large (${formatBytes(file.size)}). The limit is ${formatBytes(MAX_BYTES)}.`
          : wrongType
            ? 'Unsupported file type. Upload a PDF, Word document (.doc, .docx) or a .zip of them.'
            : undefined,
      });
    }
    setItems((current) => [...current, ...next]);
  }

  async function startUpload() {
    if (!target) return;
    const queue = items.filter((item) => item.state === 'pending');
    if (!queue.length) return;

    setBusy(true);
    setBatchError(null);
    setProgress(0);
    setItems((current) =>
      current.map((row) => (row.state === 'pending' ? { ...row, state: 'uploading' } : row)),
    );

    // Set from the upload outcome below and read after the mutation settles.
    // `items` is stale inside this closure - the setItems calls are queued, not
    // applied - so deciding whether to navigate from it would read the state as
    // it was before the upload.
    let allSucceeded = false;

    try {
      // One request for the whole batch: the endpoint takes repeated `files` parts
      // and reports a per-file outcome, so a duplicate in the middle does not
      // abandon the rest - and the browser opens one connection, not six.
      const result = await contractsApi.upload(
        target,
        queue.map((item) => item.file),
        { onProgress: setProgress },
      );

      const byName = new Map(result.files.map((entry) => [entry.file_name, entry]));
      // "Nothing was refused", not "accepted === files sent". One ZIP can produce
      // five documents, so `accepted` counts documents while `queue.length`
      // counts uploads and the two legitimately differ. Comparing them would
      // silently never navigate after an archive upload.
      //
      // Duplicates block navigation too: the duplicate notice and its link to the
      // existing contract live only on this screen.
      allSucceeded = result.rejected === 0 && result.duplicates === 0 && result.accepted > 0;

      setItems((current) =>
        current.map((row) => {
          if (row.state !== 'uploading') return row;
          const outcome = byName.get(row.file.name);
          if (!outcome) {
            return { ...row, state: 'error', message: 'No result returned for this file.' };
          }
          if (outcome.status === 'duplicate') {
            return {
              ...row,
              state: 'duplicate',
              existingContractId: outcome.existing_contract_id ?? undefined,
              message:
                outcome.message ??
                'This document is already in the project. It was not uploaded again.',
            };
          }
          // An archive has no contract of its own - it became several. Its row
          // reports how many, and which members were left out.
          if (outcome.status === 'expanded') {
            return { ...row, state: 'expanded', message: outcome.message ?? undefined };
          }
          if (outcome.contract_id) {
            return { ...row, state: 'done', contractId: outcome.contract_id };
          }
          return {
            ...row,
            state: 'error',
            message: outcome.message ?? outcome.error_code ?? 'Rejected.',
          };
        }),
      );
    } catch (caught) {
      const message = errorMessage(caught);
      setBatchError(message);
      setItems((current) =>
        current.map((row) =>
          row.state === 'uploading' ? { ...row, state: 'error', message } : row,
        ),
      );
    } finally {
      setBusy(false);
    }

    await queryClient.invalidateQueries({ queryKey: ['contracts'] });
    await queryClient.invalidateQueries({ queryKey: ['jobs'] });
    await queryClient.invalidateQueries({ queryKey: ['dashboard'] });

    // Go to the processing screen once the upload is genuinely clean. Processing
    // begins immediately, so leaving the user on a form whose work is finished
    // means the run they just started is happening on a page they are not
    // looking at.
    //
    // Only when *everything* succeeded. This screen is the sole report of what
    // went wrong - a rejection reason, or which file was a duplicate of an
    // existing contract - and none of it exists on /jobs, because a file that
    // was refused never became a job. Navigating on a partial batch would
    // discard that. The "Watch processing" button stays for exactly that case,
    // so the mixed outcome is a choice rather than a dead end.
    //
    // After the invalidations, so /jobs renders the new rows instead of a
    // cached list that predates them.
    if (allSucceeded) {
      navigate('/jobs');
    }
  }

  const uploaded = items.filter((item) => item.state === 'done');
  const queued = items.filter((item) => item.state === 'pending');
  const uploadingCount = items.filter((item) => item.state === 'uploading').length;

  if (projects.length === 0) {
    return (
      <div className="space-y-5">
        <PageHeader subtitle="Add documents to a business unit you belong to." />
        <EmptyState
          icon={FolderOpen}
          title="You are not a member of any project"
          description="Contracts live inside projects, and access is granted per project. Ask an administrator to add you to one, then this screen will let you upload into it."
        />
      </div>
    );
  }

  return (
    <div className="space-y-5">
      <PageHeader
        subtitle="Processing starts automatically: validation, parsing, clause detection, extraction, embedding and indexing."
        actions={
          uploaded.length && !busy ? (
            <Button variant="secondary" onClick={() => navigate('/jobs')}>
              Watch processing ({uploaded.length})
            </Button>
          ) : undefined
        }
      />

      <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_20rem] lg:items-start">
        <div className="space-y-5 lg:order-1">
          <Card>
            <SectionHeader
              title="Files"
              subtitle="PDF, Word (.doc, .docx) or a .zip of them. Several at once is fine."
              icon={UploadCloud}
            />

            <div
              className={[
                'flex cursor-pointer flex-col items-center justify-center rounded-2xl border-2 border-dashed px-4 py-10 text-center transition sm:px-6 sm:py-14',
                dragging
                  ? 'border-blue-500 bg-blue-50'
                  : 'border-slate-300 bg-slate-50 hover:border-blue-400 hover:bg-blue-50/40',
              ].join(' ')}
              onDragOver={(event) => {
                event.preventDefault();
                setDragging(true);
              }}
              onDragLeave={() => setDragging(false)}
              onDrop={(event) => {
                event.preventDefault();
                setDragging(false);
                if (event.dataTransfer.files.length) addFiles(event.dataTransfer.files);
              }}
              onClick={() => inputRef.current?.click()}
              role="button"
              tabIndex={0}
              onKeyDown={(event) => {
                if (event.key === 'Enter' || event.key === ' ') inputRef.current?.click();
              }}
            >
              <div className="rounded-2xl bg-blue-100 p-4 text-blue-600">
                <CloudUpload className="h-8 w-8" />
              </div>
              <p className="mt-4 text-base font-semibold text-slate-900 dark:text-slate-100">
                Drop contracts here, or tap to browse
              </p>
              <p className="mt-1 text-sm text-slate-500">
                PDF, Word or ZIP, up to {formatBytes(MAX_BYTES)} each
              </p>
              <input
                ref={inputRef}
                type="file"
                multiple
                accept={ACCEPT_ATTR}
                hidden
                onChange={(event) => {
                  if (event.target.files?.length) addFiles(event.target.files);
                  // Reset so selecting the same file twice still fires a change.
                  event.target.value = '';
                }}
              />
            </div>

            {busy ? (
              <div className="mt-4">
                <div className="h-2 w-full overflow-hidden rounded-full bg-slate-100">
                  <div
                    className="h-full rounded-full bg-blue-600 transition-all"
                    style={{ width: `${progress}%` }}
                  />
                </div>
                <p className="mt-2 text-xs text-slate-500">
                  Uploading {uploadingCount} file{uploadingCount === 1 ? '' : 's'} — {progress}%
                </p>
              </div>
            ) : null}

            {batchError ? (
              <div className="mt-4">
                <ErrorBanner message={batchError} />
              </div>
            ) : null}

            {items.length ? (
              <ul className="mt-4 space-y-2">
                {items.map((item) => (
                  <UploadRow
                    key={item.id}
                    item={item}
                    onOpen={(id) => navigate(`/contracts/${id}`)}
                    onRemove={() =>
                      setItems((current) => current.filter((row) => row.id !== item.id))
                    }
                  />
                ))}
              </ul>
            ) : null}

            <div className="mt-5 flex flex-col gap-2 sm:flex-row">
              <Button
                icon={UploadCloud}
                busy={busy}
                disabled={!target || !queued.length}
                onClick={() => void startUpload()}
                className="w-full sm:w-auto"
              >
                {busy
                  ? 'Uploading…'
                  : queued.length
                    ? `Upload ${queued.length} file${queued.length === 1 ? '' : 's'}`
                    : 'Upload'}
              </Button>
              {items.length && !busy ? (
                <Button
                  variant="ghost"
                  onClick={() => setItems([])}
                  className="w-full sm:w-auto"
                >
                  Clear list
                </Button>
              ) : null}
            </div>
          </Card>
        </div>

        {/* Destination sits above the dropzone on a phone: choosing the project is
            the first decision, and it is the one with consequences. */}
        <Card className="lg:sticky lg:top-24 lg:order-2">
          <SectionHeader
            title="Destination"
            subtitle="Which project these documents join."
            icon={FolderOpen}
          />
          <Field
            label="Project"
            required
            hint="Only members of this project will be able to see these documents."
          >
            <div className="relative">
            <select
              value={target}
              onChange={(event) => setTarget(event.target.value as UUID)}
              disabled={busy}
              className={selectClasses}
            >
              <option value="">Choose a project…</option>
              {projects.map((project) => (
                <option key={project.id} value={project.id}>
                  {project.name}
                </option>
              ))}
            </select>
            <SelectChevron />
            </div>
          </Field>
        </Card>
      </div>
    </div>
  );
}

// =============================================================================
// Row
// =============================================================================
const STATE_STYLES: Record<ItemState, { ring: string; note: string }> = {
  pending: { ring: 'border-slate-200 dark:border-slate-700', note: 'text-slate-500' },
  uploading: { ring: 'border-blue-200 bg-blue-50/40', note: 'text-blue-600' },
  done: { ring: 'border-emerald-200 bg-emerald-50/40', note: 'text-emerald-700' },
  duplicate: { ring: 'border-amber-200 bg-amber-50/40', note: 'text-amber-700' },
  // Successful, but not a contract of its own - the documents it produced are
  // the outcome, so it reads as informational rather than as a green tick.
  expanded: { ring: 'border-blue-200 bg-blue-50/40', note: 'text-blue-700' },
  error: { ring: 'border-rose-200 bg-rose-50/40', note: 'text-rose-700' },
};

function UploadRow({
  item,
  onOpen,
  onRemove,
}: {
  item: UploadItem;
  onOpen: (id: UUID) => void;
  onRemove: () => void;
}) {
  const style = STATE_STYLES[item.state];
  const openable = item.contractId ?? item.existingContractId;

  return (
    <li
      className={`flex items-center gap-3 rounded-2xl border px-3 py-3 sm:px-4 ${style.ring}`}
    >
      <div className="shrink-0 text-slate-400">
        {item.state === 'done' ? (
          <CheckCircle2 className="h-5 w-5 text-emerald-600" />
        ) : item.state === 'duplicate' ? (
          <Copy className="h-5 w-5 text-amber-600" />
        ) : item.state === 'error' ? (
          <XCircle className="h-5 w-5 text-rose-600" />
        ) : (
          <FileText className="h-5 w-5" />
        )}
      </div>

      <div className="min-w-0 flex-1">
        <div className="flex items-baseline justify-between gap-3">
          <p className="truncate text-sm font-medium text-slate-900 dark:text-slate-100">{item.file.name}</p>
          <span className="shrink-0 text-xs text-slate-500">{formatBytes(item.file.size)}</span>
        </div>
        {/* A duplicate is a normal outcome, not a failure: the same contract
            uploaded twice would otherwise become two records with two risk
            scores. */}
        {item.state === 'done' ? (
          <p className={`mt-0.5 text-xs ${style.note}`}>Uploaded — processing queued</p>
        ) : item.message ? (
          <p className={`mt-0.5 text-xs ${style.note}`}>{item.message}</p>
        ) : null}
      </div>

      {openable && (item.state === 'done' || item.state === 'duplicate') ? (
        <Button variant="secondary" size="sm" onClick={() => onOpen(openable)}>
          Open
        </Button>
      ) : item.state !== 'uploading' ? (
        <button
          type="button"
          onClick={onRemove}
          aria-label={`Remove ${item.file.name}`}
          className="shrink-0 rounded-lg p-2 text-slate-400 transition hover:bg-slate-100 hover:text-slate-700"
        >
          <Trash2 className="h-4 w-4" />
        </button>
      ) : null}
    </li>
  );
}

export default UploadPage;
