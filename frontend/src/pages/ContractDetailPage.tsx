/**
 * Contract detail - the screen the product exists for.
 *
 * Two panes side by side above `xl`: extracted knowledge on the left, the source
 * document on the right. Every assertion on the left is a click away from the words
 * it came from on the right. Nothing here is presented as a fact without a route to
 * its evidence, which is the whole difference between this and a summariser.
 *
 * Below `xl` the two panes become one, switched by a segmented control - and
 * "Show in document" flips to the document pane, so the evidence link survives on a
 * screen too narrow to hold both. Side-by-side panes on a phone would give each
 * about 180px, which is neither a readable clause list nor a legible page.
 *
 * Tabs come from the backend (`ClauseTab[]`), driven by the Clause Master, so which
 * clauses get their own tab is configuration rather than code.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  AlertTriangle,
  ArrowLeft,
  Download,
  FileSearch,
  FileText,
  Layers,
  Share2,
  Sparkles,
} from 'lucide-react';
import { useMemo, useState } from 'react';
import type { ReactNode } from 'react';
import { Link, useParams } from 'react-router-dom';

import { getAccessToken } from '@/api/client';
import {
  contracts as contractsApi,
  jobs as jobsApi,
  knowledge as knowledgeApi,
} from '@/api/endpoints';
import { errorMessage } from '@/api/errors';
import type {
  BoundingBox,
  ContractDetail,
  KeyDate,
  Obligation,
  Party,
  Risk,
  RiskAssessment,
  UUID,
} from '@/api/types';
import { ClausePanel } from '@/components/ClausePanel';
import { Badge } from '@/components/common/Badge';
import { formatStatusLabel, getRiskVariant, getStatusVariant } from '@/lib/badges';
import { ErrorBanner, NoticeBanner } from '@/components/common/Banner';
import { Button } from '@/components/common/Button';
import { Card, SectionHeader } from '@/components/common/Card';
import { EmptyState } from '@/components/common/EmptyState';
import { LoadingSpinner } from '@/components/common/LoadingSpinner';
import { CopilotDrawer } from '@/components/CopilotDrawer';
import { KnowledgeGraph } from '@/components/KnowledgeGraph';
import { PdfViewer } from '@/components/PdfViewer';
import { exportContractCsv } from '@/lib/export-csv';
import {
  daysUntil,
  formatAgreementType,
  formatDate,
  formatDateTimeFull,
  formatMoney,
  humanise,
} from '@/lib/format';

type FixedTab =
  | 'overview'
  | 'risks'
  | 'obligations'
  | 'dates'
  | 'parties'
  | 'graph'
  | 'processing';

interface Focus {
  boxes: BoundingBox[];
  page?: number | null;
  token: number;
}

/**
 * Download the source document, either the processed PDF or the original.
 *
 * Fetched rather than linked. The URL the API returns is either a pre-signed
 * storage URL, which a plain link handles, or - on every local-storage
 * deployment - an API path behind the session guard, which a link cannot
 * authenticate: the browser would navigate to it without the bearer token and
 * land on a 401 rendered as JSON. Fetching lets the same code serve both.
 */
async function downloadSource(contractId: UUID, original: boolean): Promise<void> {
  const access = original
    ? await contractsApi.fileAccessForDownload(contractId)
    : await contractsApi.fileAccess(contractId);

  const token = getAccessToken();
  const response = await fetch(access.url, {
    headers: access.is_proxied && token ? { Authorization: `Bearer ${token}` } : {},
    credentials: access.is_proxied ? 'include' : 'omit',
  });
  if (!response.ok) throw new Error(`Download failed (${response.status})`);

  const blob = await response.blob();
  const href = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = href;
  anchor.download = access.file_name;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  // Released on the next tick: revoking synchronously can cancel the download in
  // Firefox before it has read the blob.
  setTimeout(() => URL.revokeObjectURL(href), 0);
}

export function ContractDetailPage() {
  const { contractId = '' } = useParams();
  const [active, setActive] = useState<string>('overview');
  const [focus, setFocus] = useState<Focus | null>(null);
  const [pane, setPane] = useState<'knowledge' | 'document'>('knowledge');
  const [copilotOpen, setCopilotOpen] = useState(false);

  const contractQuery = useQuery({
    queryKey: ['contract', contractId],
    queryFn: () => contractsApi.get(contractId),
    enabled: Boolean(contractId),
    refetchInterval: (query) =>
      query.state.data?.status === 'processing' || query.state.data?.status === 'uploaded'
        ? 4000
        : false,
  });

  const contract = contractQuery.data;
  const ready = contract
    ? contract.status !== 'uploaded' && contract.status !== 'processing'
    : false;

  const knowledgeQuery = useQuery({
    queryKey: ['knowledge', contractId],
    queryFn: () => knowledgeApi.all(contractId),
    // Asking for knowledge before extraction has run returns an empty shell that
    // is indistinguishable from "this contract has no clauses". Wait for a state
    // where the answer is meaningful.
    enabled: Boolean(contractId) && ready,
  });

  const fileQuery = useQuery({
    queryKey: ['contract-file', contractId],
    queryFn: () => contractsApi.fileAccess(contractId),
    enabled: Boolean(contractId),
    // The URL is signed and expires; refresh comfortably before it does rather
    // than letting the viewer fail mid-read.
    staleTime: 4 * 60_000,
    refetchInterval: 4 * 60_000,
  });

  const knowledge = knowledgeQuery.data;

  // A failed contract needs its job before it can offer a retry: the retry acts
  // on the job, not the contract, and only a FAILED or CANCELLED job is
  // retryable. Fetched only on failure so the happy path costs nothing.
  const failed = contract?.status === 'failed';
  const queryClient = useQueryClient();

  const failedJobQuery = useQuery({
    queryKey: ['contract-jobs', contractId],
    queryFn: () => jobsApi.forContract(contractId),
    enabled: Boolean(contractId) && failed,
  });

  // The most recent job is the one that failed; earlier ones are history.
  const latestJob = failedJobQuery.data?.[0];

  const retry = useMutation({
    mutationFn: (jobId: string) => jobsApi.retry(jobId),
    onSuccess: () => {
      // Both change: the job leaves FAILED, and the contract leaves `failed`
      // back to `processing`, which restarts the detail poll.
      void queryClient.invalidateQueries({ queryKey: ['contract', contractId] });
      void queryClient.invalidateQueries({ queryKey: ['contract-jobs', contractId] });
    },
  });

  function showEvidence(boxes: BoundingBox[], page?: number | null) {
    setFocus({
      boxes,
      page: page ?? boxes[0]?.page_number ?? null,
      // A monotonically increasing token, so clicking the same clause twice
      // re-centres the viewer even though nothing about the target changed.
      token: (focus?.token ?? 0) + 1,
    });
    // Below `xl` only one pane is on screen; jumping to the evidence has to bring
    // the document with it or the button appears to do nothing.
    setPane('document');
  }

  const tabs = useMemo(() => {
    const fixed: Array<{ key: FixedTab; label: string; count?: number }> = [
      { key: 'overview', label: 'Overview' },
      { key: 'risks', label: 'Risks', count: knowledge?.assessment.risks.length },
      { key: 'obligations', label: 'Obligations', count: knowledge?.obligations.length },
      { key: 'dates', label: 'Key dates', count: knowledge?.key_dates.length },
      { key: 'parties', label: 'Parties', count: knowledge?.parties.length },
      // No count: the graph is built on request, so a number here would mean
      // fetching it on every visit to the contract just to label a tab.
      { key: 'graph', label: 'Graph' },
      { key: 'processing', label: 'Processing' },
    ];
    return fixed;
  }, [knowledge]);

  if (contractQuery.isLoading) return <LoadingSpinner label="Loading contract..." />;
  if (contractQuery.error) {
    return (
      <ErrorBanner
        message={errorMessage(contractQuery.error)}
        onRetry={() => void contractQuery.refetch()}
      />
    );
  }
  if (!contract) return null;

  const clauseTabs = knowledge?.tabs ?? [];
  const activeClauseTab = clauseTabs.find((tab) => tab.key === active);

  const documentPane = fileQuery.data?.url ? (
    <PdfViewer
      url={fileQuery.data.url}
      highlights={focus?.boxes ?? []}
      page={focus?.page ?? undefined}
      focusToken={focus?.token}
    />
  ) : fileQuery.error ? (
    <ErrorBanner
      message={errorMessage(fileQuery.error)}
      onRetry={() => void fileQuery.refetch()}
    />
  ) : (
    <div className="h-[32rem] animate-pulse rounded-2xl bg-slate-200" />
  );

  return (
    <div className="space-y-4">
      <Link
        to="/contracts"
        className="inline-flex items-center gap-1.5 text-sm font-medium text-slate-500 dark:text-slate-400 transition hover:text-slate-900"
      >
        <ArrowLeft className="h-4 w-4" />
        Contracts
      </Link>

      <header className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0">
          <h1 className="truncate text-xl font-semibold text-slate-900 dark:text-slate-100 sm:text-2xl">
            {contract.title ?? contract.original_file_name}
          </h1>
          
          <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-sm text-slate-500">
            <span>{formatAgreementType(contract.agreement_type)}</span>
            <span aria-hidden>·</span>
            <span>{contract.page_count ?? '—'} pages</span>
            <span aria-hidden>·</span>
            <span>Version {contract.current_version}</span>
            {contract.profile_name ? (
              <>
                <span aria-hidden>·</span>
                <span title="Document Intelligence Profile that governed extraction">
                  {contract.profile_name}
                </span>
              </>
            ) : null}
          </div>
        </div>

        <div className="flex shrink-0 flex-wrap items-center gap-2">
          <Button
            variant="secondary"
            size="sm"
            icon={Download}
            onClick={() => exportContractCsv(contract, knowledge)}
          >
            Export
          </Button>
          <Button
            size="sm"
            icon={Sparkles}
            onClick={() => setCopilotOpen(true)}
            className="bg-gradient-to-r from-blue-600 to-violet-600 text-white shadow-sm hover:from-blue-700 hover:to-violet-700"
          >
            Copilot
          </Button>
        </div>
      </header>

      {contract.status === 'processing' && contract.processing ? (
        <Card dense>
          <div className="flex items-center justify-between gap-3">
            <span className="text-sm font-medium text-slate-700">
              Processing — {humanise(contract.processing.current_stage)}
            </span>
            <span className="font-mono text-sm tabular-nums text-slate-500">
              {contract.processing.progress}%
            </span>
          </div>
          <div className="mt-2 h-2 w-full overflow-hidden rounded-full bg-slate-100">
            <div
              className="h-full rounded-full bg-blue-600 transition-all"
              style={{ width: `${contract.processing.progress}%` }}
            />
          </div>
          <p className="mt-2 text-xs text-slate-500">
            Clauses, risks and obligations appear once extraction completes. The source document
            is readable now.
          </p>
        </Card>
      ) : null}

      {failed ? (
        <ErrorBanner
          message={
            retry.isError
              ? `Retry failed: ${errorMessage(retry.error)}`
              : retry.isPending
                ? 'Retrying...'
                : latestJob?.error?.message
                  ? `Processing failed: ${latestJob.error.message}`
                  : 'Processing failed for this contract. The Processing tab has the failing stage and the full error.'
          }
          // Offered here rather than only on the Processing tab: this is where
          // the failure is seen, and a retry the user has to go and find is one
          // most users will not find. Withheld while the job is still loading -
          // a button that might do nothing is worse than one that appears a
          // moment later.
          onRetry={
            latestJob && latestJob.is_retryable && !retry.isPending
              ? () => retry.mutate(latestJob.id)
              : undefined
          }
        />
      ) : null}

      {knowledge?.needs_review ? (
        <NoticeBanner
          message={`This contract needs review: ${knowledge.review_reasons.map(humanise).join(' · ')}`}
        />
      ) : null}

      {/* Pane switch, below `xl` only. */}
      <div className="flex rounded-2xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 p-1 shadow-sm xl:hidden">
        {(
          [
            ['knowledge', 'Extracted', Layers],
            ['document', 'Document', FileSearch],
          ] as const
        ).map(([key, label, Icon]) => (
          <button
            key={key}
            type="button"
            onClick={() => setPane(key)}
            aria-pressed={pane === key}
            className={[
              'flex flex-1 items-center justify-center gap-2 rounded-xl px-3 py-2 text-sm font-semibold transition',
              pane === key
                ? 'bg-blue-600 text-white shadow-sm'
                : 'text-slate-600 hover:bg-slate-50',
            ].join(' ')}
          >
            <Icon className="h-4 w-4" />
            {label}
          </button>
        ))}
      </div>

      <div className="grid gap-4 xl:grid-cols-2 xl:items-start">
        <div
          className={['min-w-0 space-y-4', pane === 'knowledge' ? '' : 'hidden xl:block'].join(
            ' ',
          )}
        >
          <div
            role="tablist"
            aria-label="Contract sections"
            className="-mx-1 flex gap-1.5 overflow-x-auto px-1 pb-1"
          >
            {tabs.map((tab) => (
              <TabButton
                key={tab.key}
                label={tab.label}
                count={tab.count}
                active={active === tab.key}
                onClick={() => setActive(tab.key)}
              />
            ))}

            {/* Dedicated clause tabs, ordered by the Clause Master's priority.
                A mandatory category with nothing extracted still gets a tab: the
                absence is a finding, and hiding it would bury it. */}
            {clauseTabs.map((tab) => (
              <TabButton
                key={tab.key}
                label={tab.label}
                active={active === tab.key}
                missing={tab.is_missing}
                count={
                  !tab.is_missing && tab.clauses.length > 1 ? tab.clauses.length : undefined
                }
                onClick={() => setActive(tab.key)}
                title={
                  tab.is_missing ? 'Mandatory clause not found in this contract' : undefined
                }
              />
            ))}
          </div>

          <div role="tabpanel">
            {!ready && active !== 'processing' && active !== 'overview' ? (
              <EmptyState
                icon={Layers}
                title="Not extracted yet"
                description="This contract is still moving through the pipeline. This tab fills in when extraction completes."
              />
            ) : knowledgeQuery.isLoading && active !== 'overview' && active !== 'processing' ? (
              <LoadingSpinner label="Loading extracted knowledge..." />
            ) : knowledgeQuery.error && active !== 'overview' && active !== 'processing' ? (
              <ErrorBanner
                message={errorMessage(knowledgeQuery.error)}
                onRetry={() => void knowledgeQuery.refetch()}
              />
            ) : activeClauseTab ? (
              <ClausePanel
                contractId={contractId}
                tab={activeClauseTab}
                onShowEvidence={showEvidence}
              />
            ) : active === 'overview' ? (
              <OverviewTab
                contract={contract}
                summary={knowledge?.summary}
                topics={knowledge?.key_topics ?? []}
                missing={knowledge?.assessment.missing_mandatory_clauses ?? []}
                unlimited={knowledge?.assessment.has_unlimited_liability ?? false}
                onOpenTab={setActive}
              />
            ) : active === 'risks' ? (
              <RisksTab assessment={knowledge?.assessment} onShowEvidence={showEvidence} />
            ) : active === 'obligations' ? (
              <ObligationsTab
                obligations={knowledge?.obligations ?? []}
                onShowEvidence={showEvidence}
              />
            ) : active === 'dates' ? (
              <DatesTab dates={knowledge?.key_dates ?? []} onShowEvidence={showEvidence} />
            ) : active === 'parties' ? (
              <PartiesTab parties={knowledge?.parties ?? []} onShowEvidence={showEvidence} />
            ) : active === 'graph' ? (
              <GraphTab contractId={contractId} />
            ) : (
              <ProcessingTab contractId={contractId} />
            )}
          </div>
        </div>

        <aside
          className={[
            'min-w-0 xl:sticky xl:top-24',
            pane === 'document' ? '' : 'hidden xl:block',
          ].join(' ')}
        >
          {documentPane}
        </aside>
      </div>

      <CopilotDrawer
        open={copilotOpen}
        onClose={() => setCopilotOpen(false)}
        contractId={contractId}
        contractTitle={contract.title ?? contract.original_file_name}
      />
    </div>
  );
}

// =============================================================================
// Shared bits
// =============================================================================
const TabButton = ({
  label,
  count,
  active,
  missing = false,
  onClick,
  title,
}: {
  label: string;
  count?: number;
  active: boolean;
  missing?: boolean;
  onClick: () => void;
  title?: string;
}) => (
  <button
    type="button"
    role="tab"
    aria-selected={active}
    onClick={onClick}
    title={title}
    className={[
      'flex shrink-0 items-center gap-1.5 whitespace-nowrap rounded-xl px-3 py-2 text-sm font-medium transition',
      active
        ? 'bg-blue-600 text-white shadow-sm'
        : missing
          ? 'bg-amber-50 text-amber-700 ring-1 ring-inset ring-amber-200 hover:bg-amber-100'
          : 'bg-white dark:bg-slate-800 text-slate-600 dark:text-slate-300 ring-1 ring-inset ring-slate-200 hover:bg-slate-50 dark:hover:bg-slate-700',
    ].join(' ')}
  >
    {label}
    {missing ? (
      <span
        className={[
          'rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase',
          active ? 'bg-white/20' : 'bg-amber-200 text-amber-800',
        ].join(' ')}
      >
        missing
      </span>
    ) : count !== undefined && count > 0 ? (
      <span
        className={[
          'rounded px-1.5 py-0.5 text-[10px] font-semibold tabular-nums',
          active ? 'bg-white/20' : 'bg-slate-100 text-slate-600',
        ].join(' ')}
      >
        {count}
      </span>
    ) : null}
  </button>
);

/** Label-over-value pair. Two columns on a phone, three from `sm`. */
const DataField = ({ label, value }: { label: string; value: ReactNode }) => (
  <div>
    <dt className="text-xs font-semibold uppercase tracking-wider text-slate-400 dark:text-slate-400">{label}</dt>
    <dd className="mt-1 text-sm text-slate-900 dark:text-slate-100">{value}</dd>
  </div>
);

const EvidenceButton = ({
  onClick,
  label = 'Evidence',
}: {
  onClick: () => void;
  label?: string;
}) => (
  <Button variant="secondary" size="sm" icon={FileSearch} onClick={onClick}>
    {label}
  </Button>
);

// =============================================================================
// Fixed tabs
// =============================================================================
function OverviewTab({
  contract,
  summary,
  topics,
  missing,
  unlimited,
  onOpenTab,
}: {
  contract: ContractDetail;
  summary?: string | null;
  topics: string[];
  missing: string[];
  unlimited: boolean;
  onOpenTab: (key: string) => void;
}) {
  const metadata = contract.contract_metadata;
  const remaining = daysUntil(metadata?.expiration_date);

  return (
    <div className="space-y-4">
      {unlimited || missing.length ? (
        <Card>
          <SectionHeader
            title="Attention"
            subtitle="What a reviewer should look at first."
            icon={AlertTriangle}
          />
          <div className="space-y-3">
            {unlimited ? (
              <div className="rounded-2xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700">
                <p className="font-semibold">Unlimited liability exposure.</p>
                <p className="mt-1">
                  Either the liability cap is absent or carve-outs place material matters
                  outside it.
                </p>
                <button
                  type="button"
                  onClick={() => onOpenTab('limitation_of_liability')}
                  className="mt-2 font-medium underline underline-offset-2"
                >
                  Open the liability clause
                </button>
              </div>
            ) : null}
            {missing.length ? (
              <NoticeBanner
                message={`Mandatory clauses not found: ${missing.map(humanise).join(', ')}. Each absence contributes to the risk score.`}
              />
            ) : null}
          </div>
        </Card>
      ) : null}

      {summary ? (
        <Card>
          <SectionHeader title="Summary" subtitle="Generated from the extracted clauses." />
          <div className="max-h-48 overflow-y-auto pr-1">
            <p className="text-sm leading-7 text-slate-700 dark:text-slate-300">{summary}</p>
          </div>
          {topics.length ? (
            <div className="mt-4 flex flex-wrap gap-2">
              {/* {topics.map((topic) => (
                <span
                  key={topic}
                  className="rounded-full bg-slate-100 px-3 py-1 text-xs font-medium text-slate-600 dark:bg-slate-700 dark:text-slate-300"
                >
                  {humanise(topic)}
                </span>
              ))} */}
            </div>
          ) : null}
        </Card>
      ) : null}

      <Card>
        <SectionHeader
          title="Source document"
          subtitle={
            contract.has_converted_pdf
              ? 'Uploaded as a Word document and converted to PDF for processing.'
              : 'The file as uploaded.'
          }
        />
        <dl className="grid grid-cols-2 gap-4 sm:grid-cols-3">
          <DataField label="Original file" value={contract.original_file_name} />
          <DataField
            label="Uploaded as"
            value={(contract.original_file_type ?? contract.file_type ?? '—').toUpperCase()}
          />
          {/* Only shown when the two differ. Saying "Processed as PDF" under a PDF
              upload is noise that makes the interesting case harder to spot. */}
          {contract.has_converted_pdf ? (
            <DataField label="Processed as" value="PDF (converted)" />
          ) : null}
          {contract.source_archive_name ? (
            <DataField label="From archive" value={contract.source_archive_name} />
          ) : null}
        </dl>
        <div className="mt-4 flex flex-wrap gap-2">
          <Button
            variant="secondary"
            size="sm"
            icon={Download}
            onClick={() => void downloadSource(contract.id, false)}
          >
            {contract.has_converted_pdf ? 'Download PDF (processed)' : 'Download document'}
          </Button>
          {/* The original is only a separate file when it was converted. For a PDF
              upload the two are the same object, and offering both would imply a
              difference that does not exist. */}
          {contract.has_converted_pdf ? (
            <Button
              variant="secondary"
              size="sm"
              icon={Download}
              onClick={() => void downloadSource(contract.id, true)}
            >
              Download original ({(contract.original_file_type ?? '').toUpperCase()})
            </Button>
          ) : null}
        </div>
        {contract.has_converted_pdf ? (
          <p className="mt-3 text-xs text-slate-500">
            Evidence and highlights are positioned against the converted PDF, so that is
            what the viewer shows.
          </p>
        ) : null}
      </Card>

      <Card>
        <SectionHeader title="Commercial terms" subtitle="Extracted from the document." />
        <dl className="grid grid-cols-2 gap-4 sm:grid-cols-3">
          <DataField label="Party A" value={metadata?.party_a ?? '—'} />
          <DataField label="Party B" value={metadata?.party_b ?? '—'} />
          <DataField
            label="Value"
            value={formatMoney(metadata?.contract_value, metadata?.currency)}
          />
          <DataField label="Effective" value={formatDate(metadata?.effective_date)} />
          <DataField label="Executed" value={formatDate(metadata?.execution_date)} />
          <DataField
            label="Expires"
            value={
              <>
                {formatDate(metadata?.expiration_date)}
                {remaining !== null ? (
                  <span className="ml-1.5 text-xs text-slate-500 dark:text-slate-400">
                    {remaining < 0 ? 'expired' : `in ${remaining} days`}
                  </span>
                ) : null}
              </>
            }
          />
          <DataField
            label="Term"
            value={metadata?.term_months ? `${metadata.term_months} months` : '—'}
          />
          <DataField label="Governing law" value={metadata?.governing_law ?? '—'} />
          <DataField label="Jurisdiction" value={metadata?.jurisdiction ?? '—'} />
          <DataField
            label="Payment terms"
            value={metadata?.payment_terms_days ? `${metadata.payment_terms_days} days` : '—'}
          />
          <DataField
            label="Auto-renewal"
            value={
              metadata?.auto_renewal
                ? `Yes${metadata.auto_renewal_notice_days ? ` — ${metadata.auto_renewal_notice_days} days notice` : ''}`
                : metadata?.auto_renewal === false
                  ? 'No'
                  : 'No'
            }
          />
          <DataField label="Language" value={contract.language?.toUpperCase() ?? '—'} />
        </dl>
      </Card>

      <Card>
        <SectionHeader title="Document" subtitle="The file this knowledge came from." />
        <dl className="grid grid-cols-2 gap-4 sm:grid-cols-3">
          <DataField label="Name" value={<span className="break-all">{contract.original_file_name}</span>} />
          <DataField label="Pages" value={contract.page_count ?? '—'} />
          <DataField label="Uploaded On" value={formatDateTimeFull(contract.created_at)} />
        </dl>
      </Card>
    </div>
  );
}

function RisksTab({
  assessment,
  onShowEvidence,
}: {
  assessment?: RiskAssessment;
  onShowEvidence: (boxes: BoundingBox[], page?: number | null) => void;
}) {
  if (!assessment) return <LoadingSpinner label="Loading risk assessment..." />;
  if (assessment.risks.length === 0) {
    return (
      <EmptyState
        icon={AlertTriangle}
        title="No risks identified"
        description="No clause in this contract matched a risk rule, and no mandatory clause is missing."
      />
    );
  }

  const order = { critical: 0, high: 1, medium: 2, low: 3 } as const;
  const sorted = [...assessment.risks].sort(
    (a, b) => (order[a.severity] ?? 9) - (order[b.severity] ?? 9),
  );

  return (
    <div className="space-y-4">
      <Card>
        <div className="flex flex-wrap items-center gap-4">
            <span className="text-3xl font-semibold tabular-nums text-slate-900 dark:text-slate-100">
            {assessment.score}
          </span>
          <Badge
            text={humanise(assessment.band) || '—'}
            variant={getRiskVariant(assessment.band)}
            size="md"
          />
          <div className="ml-auto flex flex-wrap gap-2">
            {Object.entries(assessment.by_severity).map(([severity, count]) => (
              <Badge
                key={severity}
                text={`${humanise(severity)} ${count}`}
                variant={getRiskVariant(severity)}
              />
            ))}
          </div>
        </div>

        {/* The score decomposes: every point is attributable to a named finding.
            An unexplained number is not a number a lawyer will act on. */}
        {assessment.breakdown.length ? (
          <div className="-mx-6 mt-5 overflow-x-auto px-6">
            <table className="w-full min-w-[30rem] text-left text-sm">
              <thead className="border-b border-slate-200 dark:border-slate-700 text-xs uppercase tracking-wider text-slate-500 dark:text-slate-400">
                <tr>
                  <th scope="col" className="py-2 pr-4 font-semibold">Finding</th>
                  <th scope="col" className="py-2 pr-4 font-semibold">Severity</th>
                  <th scope="col" className="py-2 pr-4 text-right font-semibold">Weight</th>
                  <th scope="col" className="py-2 text-right font-semibold">Applied</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {assessment.breakdown.map((row, index) => (
                  <tr key={index}>
                    <td className="py-2 pr-4 text-slate-700">
                      {row.description}
                      {row.is_omission ? (
                        <Badge text="Absence" variant="warning" className="ml-2" />
                      ) : null}
                    </td>
                    <td className="py-2 pr-4">
                      <Badge
                        text={humanise(row.severity)}
                        variant={getRiskVariant(row.severity)}
                      />
                    </td>
                    <td className="py-2 pr-4 text-right tabular-nums text-slate-600">
                      {row.weight}
                    </td>
                    <td className="py-2 text-right tabular-nums text-slate-600">
                      {row.applied}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : null}
      </Card>

      {sorted.map((risk) => (
        <RiskCard key={risk.id} risk={risk} onShowEvidence={onShowEvidence} />
      ))}
    </div>
  );
}

function RiskCard({
  risk,
  onShowEvidence,
}: {
  risk: Risk;
  onShowEvidence: (boxes: BoundingBox[], page?: number | null) => void;
}) {
  return (
    <Card dense>
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="flex flex-wrap items-center gap-2">
          <Badge text={humanise(risk.severity)} variant={getRiskVariant(risk.severity)} />
          <span className="font-semibold text-slate-900 dark:text-slate-100">{humanise(risk.risk_type)}</span>
        </div>
        {/* An omission has no coordinates by definition, so no evidence button is
            offered - the evidence is that nothing matched anywhere. */}
        {risk.is_omission ? (
          <Badge text="Absent from contract" variant="warning" />
        ) : risk.bounding_boxes.length ? (
          <EvidenceButton
            label="Evidence"
            onClick={() => onShowEvidence(risk.bounding_boxes, risk.page_start)}
          />
        ) : null}
      </div>
      <p className="mt-3 text-sm leading-6 text-slate-700">{risk.description}</p>
      {risk.recommendation ? (
        <p className="mt-3 rounded-xl bg-blue-50 px-3 py-2 text-sm leading-6 text-blue-900">
          <span className="font-semibold">Recommendation:</span> {risk.recommendation}
        </p>
      ) : null}
    </Card>
  );
}

function ObligationsTab({
  obligations,
  onShowEvidence,
}: {
  obligations: Obligation[];
  onShowEvidence: (boxes: BoundingBox[], page?: number | null) => void;
}) {
  if (obligations.length === 0) {
    return (
      <EmptyState
        icon={Layers}
        title="No obligations extracted"
        description="Nothing in this contract was identified as a dated or triggered commitment."
      />
    );
  }

  return (
    <div className="space-y-3">
      {obligations.map((obligation) => (
        <Card key={obligation.id} dense>
          <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
            <div className="min-w-0">
              <p className="text-sm font-medium text-slate-900 dark:text-slate-100">{obligation.action}</p>
              {obligation.trigger_event ? (
                <p className="mt-1 text-xs text-slate-500">
                  Trigger: {obligation.trigger_event}
                </p>
              ) : null}
            </div>
            {obligation.bounding_boxes.length ? (
              <EvidenceButton
                onClick={() => onShowEvidence(obligation.bounding_boxes, obligation.page_start)}
              />
            ) : null}
          </div>
          <dl className="mt-3 grid grid-cols-2 gap-3 border-t border-slate-100 pt-3">
            <DataField label="Responsible" value={obligation.responsible_party ?? '—'} />
            <DataField
              label="Due"
              value={
                <>
                  {/* A relative deadline is kept in its own words: "within 30 days of
                      termination" is the obligation, and resolving it to a date the
                      contract does not state would be an invention. */}
                  {obligation.due_date
                    ? formatDate(obligation.due_date)
                    : (obligation.due_description ?? '—')}
                  {obligation.is_recurring ? (
                    <Badge text="Recurring" variant="neutral" className="ml-2" />
                  ) : null}
                </>
              }
            />
          </dl>
        </Card>
      ))}
    </div>
  );
}

function DatesTab({
  dates,
  onShowEvidence,
}: {
  dates: KeyDate[];
  onShowEvidence: (boxes: BoundingBox[], page?: number | null) => void;
}) {
  if (dates.length === 0) {
    return (
      <EmptyState
        icon={Layers}
        title="No key dates extracted"
        description="No effective, expiry, renewal or notice dates were identified in this contract."
      />
    );
  }

  return (
    <div className="space-y-3">
      {dates.map((date) => (
        <Card key={date.id} dense>
          <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
            <div className="min-w-0">
              <div className="flex flex-wrap items-center gap-2">
                <Badge text={humanise(date.date_type)} variant="info" />
                <span className="text-sm font-medium text-slate-900 dark:text-slate-100">
                  {date.date_value
                    ? formatDate(date.date_value)
                    : (date.date_expression ?? '—')}
                </span>
              </div>
              {date.description ? (
                <p className="mt-1.5 text-sm leading-6 text-slate-600">{date.description}</p>
              ) : null}
            </div>
            {date.bounding_boxes.length ? (
              <EvidenceButton
                onClick={() => onShowEvidence(date.bounding_boxes, date.page_start)}
              />
            ) : null}
          </div>
        </Card>
      ))}
    </div>
  );
}

function PartiesTab({
  parties,
  onShowEvidence,
}: {
  parties: Party[];
  onShowEvidence: (boxes: BoundingBox[], page?: number | null) => void;
}) {
  if (parties.length === 0) {
    return (
      <EmptyState
        icon={Layers}
        title="No parties extracted"
        description="No contracting entities were identified in this document."
      />
    );
  }

  return (
    <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-1 2xl:grid-cols-2">
      {parties.map((party) => (
        <Card key={party.id} dense>
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0">
              <p className="truncate font-semibold text-slate-900 dark:text-slate-100">{party.name}</p>
              {party.legal_name && party.legal_name !== party.name ? (
                <p className="truncate text-xs text-slate-500">{party.legal_name}</p>
              ) : null}
            </div>
            {party.is_primary ? <Badge text="Primary" variant="info" /> : null}
          </div>
          <dl className="mt-3 grid grid-cols-2 gap-3">
            <DataField label="Role" value={humanise(party.role)} />
            <DataField label="Type" value={humanise(party.entity_type)} />
            <DataField label="Jurisdiction" value={party.jurisdiction ?? '—'} />
          </dl>
          {party.bounding_boxes.length ? (
            <div className="mt-3">
              <EvidenceButton
                label="Evidence"
                onClick={() => onShowEvidence(party.bounding_boxes, party.page_start)}
              />
            </div>
          ) : null}
        </Card>
      ))}
    </div>
  );
}

/**
 * Graph tab.
 *
 * Fetched only when the tab is opened, and never refetched on its own: the graph
 * is rebuilt server-side from six row sets, so polling it would be pure cost for a
 * picture that only changes when somebody reprocesses or reviews the contract.
 *
 * A contract still being processed answers with a partial graph rather than an
 * error, which is why an empty result is presented as "nothing extracted yet"
 * instead of a failure.
 */
function GraphTab({ contractId }: { contractId: string }) {
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ['contract-graph', contractId],
    queryFn: () => knowledgeApi.graph(contractId),
    staleTime: 5 * 60_000,
  });

  if (isLoading) return <LoadingSpinner label="Building the graph..." />;
  if (error) {
    return <ErrorBanner message={errorMessage(error)} onRetry={() => void refetch()} />;
  }
  if (!data || !data.nodes.length) {
    return (
      <EmptyState
        icon={Share2}
        title="Nothing to graph yet"
        description="The graph is built from the parties, clauses, obligations and risks extracted from this contract. It fills in once processing has run."
      />
    );
  }
  return <KnowledgeGraph graph={data} />;
}

/**
 * Processing tab.
 *
 * Stage-level visibility, including which stages reused a checkpoint rather than
 * re-running. Reprocessing from a chosen stage is here because that is the natural
 * response to "extraction got this wrong" - and it is far cheaper than re-parsing.
 */
function ProcessingTab({ contractId }: { contractId: string }) {
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ['contract-jobs', contractId],
    queryFn: () => jobsApi.forContract(contractId),
    refetchInterval: (query) =>
      query.state.data?.some((job) => !['ready', 'failed', 'cancelled'].includes(job.state))
        ? 4000
        : false,
  });

  if (isLoading) return <LoadingSpinner label="Loading processing runs..." />;
  if (error) {
    return <ErrorBanner message={errorMessage(error)} onRetry={() => void refetch()} />;
  }
  if (!data?.length) {
    return (
      <EmptyState
        icon={FileText}
        title="No processing runs for this contract"
        description="A run is created when a contract is uploaded or reprocessed."
      />
    );
  }

  return (
    <div className="space-y-4">
      {data.map((job) => (
        <Card key={job.id} dense>
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="flex items-center gap-2">
              <Badge
                text={formatStatusLabel(job.state)}
                variant={getStatusVariant(job.state)}
              />
              <span className="font-mono text-xs text-slate-400">{job.id.slice(0, 8)}</span>
            </div>
            <Link
              to={`/jobs?job=${job.id}`}
              className="text-sm font-medium text-blue-600 hover:text-blue-700"
            >
              Open in processing
            </Link>
          </div>

          <div className="mt-3 h-2 w-full overflow-hidden rounded-full bg-slate-100">
            <div
              className="h-full rounded-full bg-blue-600 transition-all"
              style={{ width: `${job.progress}%` }}
            />
          </div>

          {job.error ? (
            <div className="mt-3">
              <ErrorBanner
                message={String(job.error.message ?? job.error.code ?? 'Processing failed')}
              />
            </div>
          ) : null}

          <div className="-mx-5 mt-4 overflow-x-auto px-5">
            <table className="w-full min-w-[26rem] text-left text-sm">
              <thead className="border-b border-slate-200 dark:border-slate-700 text-xs uppercase tracking-wider text-slate-500 dark:text-slate-400">
                <tr>
                  <th scope="col" className="py-2 pr-4 font-semibold">Stage</th>
                  <th scope="col" className="py-2 pr-4 font-semibold">Status</th>
                  <th scope="col" className="py-2 pr-4 text-right font-semibold">Attempt</th>
                  <th scope="col" className="py-2 text-right font-semibold">Duration</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {job.stages.map((stage) => (
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
                            title="Reused a valid artifact instead of re-running"
                          />
                        ) : null}
                      </div>
                    </td>
                    <td className="py-2 pr-4 text-right tabular-nums text-slate-600">
                      {stage.attempt}
                    </td>
                    <td className="py-2 text-right tabular-nums text-slate-600">
                      {stage.duration_ms !== null && stage.duration_ms !== undefined
                        ? `${(stage.duration_ms / 1000).toFixed(1)}s`
                        : '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      ))}
    </div>
  );
}

export default ContractDetailPage;
