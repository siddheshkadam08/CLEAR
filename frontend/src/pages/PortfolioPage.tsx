/**
 * The registers: obligations, key dates, risks and counterparties, read across
 * every contract rather than one at a time.
 *
 * Extraction has always produced these rows and the contract detail screen has
 * always shown them - for one agreement. That answers "what is in this contract?"
 * and nothing else. It cannot answer "what falls due this month?", "which
 * counterparty carries the most exposure?" or "where are the uncapped liability
 * clauses?", which is what a repository is kept for.
 *
 * One screen with four tabs rather than four nav entries: they share a scope
 * selector and a mental model ("the register"), and four more items in a sidebar
 * that already has ten makes the common path harder to find.
 *
 * Two decisions repeat across the tabs:
 *
 * - **Unresolved rows are shown, not hidden.** An obligation whose deadline the
 *   contract states relatively ("within 30 days of invoice") has no calendar date,
 *   and a register that dropped it would silently shrink the list it exists to be
 *   complete about. Those rows sort last and have their own filter, which makes
 *   them a review queue instead of a gap.
 * - **Every row links back to its contract.** A register is a pointer; the
 *   evidence and the highlight live on the document.
 */

import { keepPreviousData, useQuery } from '@tanstack/react-query';
import {
  AlertOctagon,
  CalendarClock,
  ListChecks,
  Search,
  Users,
} from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import { useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';

import { portfolio as portfolioApi } from '@/api/endpoints';
import { errorMessage } from '@/api/errors';
import type {
  DateType,
  ObligationStatus,
  PartyDirectoryEntry,
  PortfolioKeyDate,
  PortfolioObligation,
  PortfolioRisk,
  RiskSeverity,
} from '@/api/types';
import { Badge } from '@/components/common/Badge';
import type { BadgeVariant } from '@/components/common/Badge';
import { ErrorBanner } from '@/components/common/Banner';
import { Card, PageHeader } from '@/components/common/Card';
import { EmptyState } from '@/components/common/EmptyState';
import { FilterChip } from '@/components/common/FilterChip';
import { inputClasses } from '@/components/common/Field';
import { LoadingSpinner } from '@/components/common/LoadingSpinner';
import { Pagination } from '@/components/common/Pagination';
import { daysUntil, formatDate, formatMoney, humanise } from '@/lib/format';
import { useProjectScope } from '@/lib/scope';

const PAGE_SIZE = 25;

type TabName = 'obligations' | 'key-dates' | 'risks' | 'parties';

const TABS: { name: TabName; label: string; icon: LucideIcon }[] = [
  { name: 'obligations', label: 'Obligations', icon: ListChecks },
  { name: 'key-dates', label: 'Key dates', icon: CalendarClock },
  { name: 'risks', label: 'Risks', icon: AlertOctagon },
  { name: 'parties', label: 'Counterparties', icon: Users },
];

const OBLIGATION_STATUSES: ObligationStatus[] = [
  'open',
  'in_progress',
  'fulfilled',
  'breached',
  'waived',
];
const SEVERITIES: RiskSeverity[] = ['critical', 'high', 'medium', 'low'];
const DATE_TYPES: DateType[] = [
  'expiration_date',
  'renewal_date',
  'notice_deadline',
  'payment_due',
  'milestone',
  'review_date',
  'termination_date',
];

const SEVERITY_VARIANT: Record<RiskSeverity, BadgeVariant> = {
  critical: 'danger',
  high: 'danger',
  medium: 'warning',
  low: 'neutral',
};

const OBLIGATION_VARIANT: Record<string, BadgeVariant> = {
  open: 'warning',
  in_progress: 'info',
  fulfilled: 'success',
  breached: 'danger',
  waived: 'neutral',
  unknown: 'neutral',
};

export function PortfolioPage() {
  const [params, setParams] = useSearchParams();
  const raw = params.get('tab');
  const tab: TabName = TABS.some((entry) => entry.name === raw) ? (raw as TabName) : 'obligations';

  return (
    <div className="space-y-5">
      <PageHeader
        title="Portfolio"
        subtitle="Obligations, dates, risks and counterparties across every contract you can see."
      />

      <div className="flex flex-wrap gap-1 border-b border-slate-200 dark:border-slate-700">
        {TABS.map(({ name, label, icon: Icon }) => (
          <button
            key={name}
            type="button"
            aria-current={tab === name ? 'page' : undefined}
            onClick={() => {
              // Filters belong to the tab that defined them; carrying `severity`
              // into the obligations tab would 422.
              const next = new URLSearchParams();
              if (name !== 'obligations') next.set('tab', name);
              setParams(next, { replace: true });
            }}
            className={[
              '-mb-px flex items-center gap-2 border-b-2 px-4 py-2.5 text-sm font-medium transition',
              tab === name
                ? 'border-blue-600 text-blue-700 dark:text-blue-400'
                : 'border-transparent text-slate-500 hover:text-slate-800 dark:hover:text-slate-200',
            ].join(' ')}
          >
            <Icon className="h-4 w-4" />
            {label}
          </button>
        ))}
      </div>

      {tab === 'obligations' ? <ObligationsTab /> : null}
      {tab === 'key-dates' ? <KeyDatesTab /> : null}
      {tab === 'risks' ? <RisksTab /> : null}
      {tab === 'parties' ? <PartiesTab /> : null}
    </div>
  );
}

// =============================================================================
// Shared pieces
// =============================================================================
/** Reads and writes one filter through the query string, so a view is linkable. */
function useFilters() {
  const [params, setParams] = useSearchParams();
  const page = Number(params.get('page') ?? 1);

  function toggle(key: string, value: string) {
    const next = new URLSearchParams(params);
    const current = next.getAll(key);
    next.delete(key);
    for (const entry of current.includes(value)
      ? current.filter((item) => item !== value)
      : [...current, value]) {
      next.append(key, entry);
    }
    next.delete('page');
    setParams(next, { replace: true });
  }

  function set(key: string, value: string | null) {
    const next = new URLSearchParams(params);
    if (value) next.set(key, value);
    else next.delete(key);
    next.delete('page');
    setParams(next, { replace: true });
  }

  function goToPage(value: number) {
    const next = new URLSearchParams(params);
    next.set('page', String(value));
    setParams(next);
  }

  return { params, page, toggle, set, goToPage };
}


function SearchBox({
  placeholder,
  value,
  onCommit,
}: {
  placeholder: string;
  value: string;
  onCommit: (next: string) => void;
}) {
  const [draft, setDraft] = useState(value);
  return (
    <div className="relative sm:min-w-[16rem] sm:flex-1">
      <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-400" />
      <input
        type="search"
        placeholder={placeholder}
        value={draft}
        onChange={(event) => setDraft(event.target.value)}
        // Committed on Enter rather than on every keystroke: each change is a
        // request, and these queries join across the whole estate.
        onKeyDown={(event) => {
          if (event.key === 'Enter') onCommit(draft.trim());
        }}
        onBlur={() => onCommit(draft.trim())}
        className={`${inputClasses} pl-9`}
      />
    </div>
  );
}

/** The contract this row belongs to, as a link. */
function ContractLink({ row }: { row: { contract_id: string; contract_title?: string | null; contract_number?: string | null } }) {
  return (
    <Link
      to={`/contracts/${row.contract_id}`}
      className="font-medium text-blue-600 hover:text-blue-700"
    >
      {row.contract_title ?? 'Untitled contract'}
      {row.contract_number ? (
        <span className="ml-1.5 font-normal text-slate-400">{row.contract_number}</span>
      ) : null}
    </Link>
  );
}

function DueLabel({ value }: { value?: number | string | null }) {
  const remaining = daysUntil(value);
  if (remaining === null) return null;
  const overdue = remaining < 0;
  return (
    <span className={overdue ? 'text-rose-600' : remaining <= 14 ? 'text-amber-600' : undefined}>
      {formatDate(value)}
      {overdue ? ` · ${Math.abs(remaining)} days overdue` : ` · in ${remaining} days`}
    </span>
  );
}

function RegisterShell({
  isLoading,
  error,
  onRetry,
  count,
  page,
  pages,
  total,
  onPage,
  empty,
  children,
}: {
  isLoading: boolean;
  error: unknown;
  onRetry: () => void;
  count: number;
  page: number;
  pages: number;
  total: number;
  onPage: (next: number) => void;
  empty: React.ReactNode;
  children: React.ReactNode;
}) {
  if (error) return <ErrorBanner message={errorMessage(error)} onRetry={onRetry} />;
  if (isLoading) {
    return (
      <Card>
        <LoadingSpinner label="Loading..." />
      </Card>
    );
  }
  if (!count) return <>{empty}</>;
  return (
    <>
      <div className="space-y-2">{children}</div>
      {pages > 1 ? (
        <Card dense>
          <Pagination page={page} pages={pages} total={total} pageSize={PAGE_SIZE} onPage={onPage} />
        </Card>
      ) : null}
    </>
  );
}

// =============================================================================
// Obligations
// =============================================================================
function ObligationsTab() {
  const { projectId } = useProjectScope();
  const { params, page, toggle, set, goToPage } = useFilters();

  const statuses = params.getAll('status') as ObligationStatus[];
  const party = params.get('responsible_party') ?? '';
  const q = params.get('q') ?? '';
  const undated = params.get('undated');

  const query = useQuery({
    queryKey: ['portfolio-obligations', projectId, params.toString()],
    queryFn: () =>
      portfolioApi.obligations({
        page,
        size: PAGE_SIZE,
        project_id: projectId,
        status: statuses,
        responsible_party: party || undefined,
        q: q || undefined,
        undated: undated === null ? undefined : undated === 'true',
      }),
    placeholderData: keepPreviousData,
  });

  const data = query.data;

  return (
    <>
      <Card dense>
        <div className="flex flex-col gap-3">
          <div className="flex flex-wrap gap-2">
            {OBLIGATION_STATUSES.map((status) => (
              <FilterChip
                key={status}
                label={humanise(status)}
                active={statuses.includes(status)}
                onClick={() => toggle('status', status)}
              />
            ))}
            <span className="mx-1 w-px self-stretch bg-slate-200" />
            <FilterChip
              label="No resolved date"
              active={undated === 'true'}
              onClick={() => set('undated', undated === 'true' ? null : 'true')}
            />
          </div>
          <div className="flex flex-col gap-2 sm:flex-row">
            <SearchBox
              placeholder="Search the obligation text"
              value={q}
              onCommit={(next) => set('q', next || null)}
            />
            <SearchBox
              placeholder="Responsible party"
              value={party}
              onCommit={(next) => set('responsible_party', next || null)}
            />
          </div>
        </div>
      </Card>

      <RegisterShell
        isLoading={query.isLoading}
        error={query.error}
        onRetry={() => void query.refetch()}
        count={data?.items.length ?? 0}
        page={data?.meta.page ?? 1}
        pages={data?.meta.pages ?? 1}
        total={data?.meta.total ?? 0}
        onPage={goToPage}
        empty={
          <EmptyState
            icon={ListChecks}
            title="No obligations match"
            description="Obligations are extracted from each contract as it is processed. Widen the filters, or process a contract to populate the register."
          />
        }
      >
        {(data?.items ?? []).map((row) => (
          <ObligationRow key={row.id} row={row} />
        ))}
      </RegisterShell>
    </>
  );
}

function ObligationRow({ row }: { row: PortfolioObligation }) {
  return (
    <Card dense>
      <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <Badge text={humanise(row.status)} variant={OBLIGATION_VARIANT[row.status] ?? 'neutral'} />
            {row.is_recurring ? <Badge text="Recurring" variant="info" /> : null}
            {row.responsible_party ? (
              <span className="text-sm font-semibold text-slate-900 dark:text-slate-100">
                {row.responsible_party}
              </span>
            ) : null}
          </div>
          <p className="mt-1.5 text-sm leading-6 text-slate-700 dark:text-slate-200">{row.action}</p>
          <div className="mt-1.5 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-slate-500">
            <ContractLink row={row} />
            {row.due_date ? (
              <DueLabel value={row.due_date} />
            ) : row.due_description ? (
              // The contract's own wording. Shown as-is because that phrasing *is*
              // the deadline; a computed date would be an invention.
              <span className="italic">{row.due_description}</span>
            ) : (
              <span className="text-slate-400">No deadline stated</span>
            )}
            {row.trigger_event ? <span>Triggered by {row.trigger_event}</span> : null}
            {row.frequency ? <span>{humanise(row.frequency)}</span> : null}
          </div>
          {row.penalty ? (
            <p className="mt-2 text-xs text-rose-700">Penalty: {row.penalty}</p>
          ) : null}
        </div>
      </div>
    </Card>
  );
}

// =============================================================================
// Key dates
// =============================================================================
function KeyDatesTab() {
  const { projectId } = useProjectScope();
  const { params, page, toggle, set, goToPage } = useFilters();

  const types = params.getAll('date_type') as DateType[];
  const unresolved = params.get('unresolved');

  const query = useQuery({
    queryKey: ['portfolio-key-dates', projectId, params.toString()],
    queryFn: () =>
      portfolioApi.keyDates({
        page,
        size: PAGE_SIZE,
        project_id: projectId,
        date_type: types,
        unresolved: unresolved === null ? undefined : unresolved === 'true',
      }),
    placeholderData: keepPreviousData,
  });

  const data = query.data;

  return (
    <>
      <Card dense>
        <div className="flex flex-wrap gap-2">
          {DATE_TYPES.map((type) => (
            <FilterChip
              key={type}
              label={humanise(type)}
              active={types.includes(type)}
              onClick={() => toggle('date_type', type)}
            />
          ))}
          <span className="mx-1 w-px self-stretch bg-slate-200" />
          <FilterChip
            label="Unresolved wording"
            active={unresolved === 'true'}
            onClick={() => set('unresolved', unresolved === 'true' ? null : 'true')}
          />
        </div>
      </Card>

      <RegisterShell
        isLoading={query.isLoading}
        error={query.error}
        onRetry={() => void query.refetch()}
        count={data?.items.length ?? 0}
        page={data?.meta.page ?? 1}
        pages={data?.meta.pages ?? 1}
        total={data?.meta.total ?? 0}
        onPage={goToPage}
        empty={
          <EmptyState
            icon={CalendarClock}
            title="No key dates match"
            description="Effective dates, expiries, renewal windows and payment milestones appear here as contracts are processed."
          />
        }
      >
        {(data?.items ?? []).map((row) => (
          <KeyDateRow key={row.id} row={row} />
        ))}
      </RegisterShell>
    </>
  );
}

function KeyDateRow({ row }: { row: PortfolioKeyDate }) {
  return (
    <Card dense>
      <div className="flex flex-col gap-2 sm:flex-row sm:items-baseline sm:gap-4">
        <div className="w-full shrink-0 sm:w-48">
          {row.date_value ? (
            <span className="text-sm font-semibold tabular-nums text-slate-900 dark:text-slate-100">
              <DueLabel value={row.date_value} />
            </span>
          ) : (
            <span className="text-sm italic text-slate-500">Not resolved</span>
          )}
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <Badge text={humanise(row.date_type)} variant="neutral" />
            {row.is_recurring ? <Badge text="Recurring" variant="info" /> : null}
          </div>
          {row.description ? (
            <p className="mt-1.5 text-sm text-slate-700 dark:text-slate-200">{row.description}</p>
          ) : null}
          {/* Only when there is no calendar date: otherwise the two would compete
              and the reader would not know which is authoritative. */}
          {!row.date_value && row.date_expression ? (
            <p className="mt-1 text-sm italic text-slate-600">“{row.date_expression}”</p>
          ) : null}
          <div className="mt-1.5 text-xs text-slate-500">
            <ContractLink row={row} />
          </div>
        </div>
      </div>
    </Card>
  );
}

// =============================================================================
// Risks
// =============================================================================
function RisksTab() {
  const { projectId } = useProjectScope();
  const { params, page, toggle, set, goToPage } = useFilters();

  const severities = params.getAll('severity') as RiskSeverity[];
  const omissions = params.get('omissions');
  const q = params.get('q') ?? '';

  const query = useQuery({
    queryKey: ['portfolio-risks', projectId, params.toString()],
    queryFn: () =>
      portfolioApi.risks({
        page,
        size: PAGE_SIZE,
        project_id: projectId,
        severity: severities,
        omissions: omissions === null ? undefined : omissions === 'true',
        q: q || undefined,
      }),
    placeholderData: keepPreviousData,
  });

  const data = query.data;

  return (
    <>
      <Card dense>
        <div className="flex flex-col gap-3">
          <div className="flex flex-wrap gap-2">
            {SEVERITIES.map((severity) => (
              <FilterChip
                key={severity}
                label={humanise(severity)}
                active={severities.includes(severity)}
                onClick={() => toggle('severity', severity)}
              />
            ))}
            <span className="mx-1 w-px self-stretch bg-slate-200" />
            <FilterChip
              label="Missing clauses only"
              active={omissions === 'true'}
              onClick={() => set('omissions', omissions === 'true' ? null : 'true')}
            />
          </div>
          <SearchBox
            placeholder="Search findings and recommendations"
            value={q}
            onCommit={(next) => set('q', next || null)}
          />
        </div>
      </Card>

      <RegisterShell
        isLoading={query.isLoading}
        error={query.error}
        onRetry={() => void query.refetch()}
        count={data?.items.length ?? 0}
        page={data?.meta.page ?? 1}
        pages={data?.meta.pages ?? 1}
        total={data?.meta.total ?? 0}
        onPage={goToPage}
        empty={
          <EmptyState
            icon={AlertOctagon}
            title="No risks match"
            description="Findings are attributed to the clause that creates them as each contract is analysed."
          />
        }
      >
        {(data?.items ?? []).map((row) => (
          <RiskRow key={row.id} row={row} />
        ))}
      </RegisterShell>
    </>
  );
}

function RiskRow({ row }: { row: PortfolioRisk }) {
  return (
    <Card
      dense
      className={row.severity === 'critical' ? 'border-l-4 border-l-rose-500' : ''}
    >
      <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <Badge text={humanise(row.severity)} variant={SEVERITY_VARIANT[row.severity]} />
            <Badge text={humanise(row.risk_type)} variant="neutral" />
            {/* Worth its own badge: an omission has no clause to point at, so a
                reader looking for the highlighted text would find nothing. */}
            {row.is_omission ? <Badge text="Absent clause" variant="warning" /> : null}
            {row.category ? (
              <span className="text-xs text-slate-500">{humanise(row.category)}</span>
            ) : null}
          </div>
          <p className="mt-1.5 text-sm leading-6 text-slate-700 dark:text-slate-200">
            {row.description}
          </p>
          {row.recommendation ? (
            <p className="mt-1.5 text-xs text-slate-600 dark:text-slate-300">
              <span className="font-semibold">Recommended:</span> {row.recommendation}
            </p>
          ) : null}
          <div className="mt-1.5 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-slate-500">
            <ContractLink row={row} />
            {row.contract_risk_score != null ? (
              <span>Contract scores {row.contract_risk_score}/100</span>
            ) : null}
            {row.score_contribution != null ? (
              <span>Contributes {row.score_contribution}</span>
            ) : null}
          </div>
        </div>
      </div>
    </Card>
  );
}

// =============================================================================
// Counterparties
// =============================================================================
function PartiesTab() {
  const { projectId } = useProjectScope();
  const { params, page, set, goToPage } = useFilters();

  const q = params.get('q') ?? '';
  const primaryOnly = params.get('primary_only') === 'true';

  const query = useQuery({
    queryKey: ['portfolio-parties', projectId, params.toString()],
    queryFn: () =>
      portfolioApi.parties({
        page,
        size: PAGE_SIZE,
        project_id: projectId,
        q: q || undefined,
        primary_only: primaryOnly || undefined,
      }),
    placeholderData: keepPreviousData,
  });

  const data = query.data;

  return (
    <>
      <Card dense>
        <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
          <SearchBox
            placeholder="Search counterparty names"
            value={q}
            onCommit={(next) => set('q', next || null)}
          />
          <FilterChip
            label="Signatories only"
            active={primaryOnly}
            onClick={() => set('primary_only', primaryOnly ? null : 'true')}
          />
        </div>
        <p className="mt-2 text-xs text-slate-500">
          Grouped by exact name. Spelling variants of the same company — “Acme Corp” and “Acme
          Corporation Inc.” — are listed separately, because merging them would be a guess and
          would under-report exposure.
        </p>
      </Card>

      <RegisterShell
        isLoading={query.isLoading}
        error={query.error}
        onRetry={() => void query.refetch()}
        count={data?.items.length ?? 0}
        page={data?.meta.page ?? 1}
        pages={data?.meta.pages ?? 1}
        total={data?.meta.total ?? 0}
        onPage={goToPage}
        empty={
          <EmptyState
            icon={Users}
            title="No counterparties match"
            description="Parties are extracted from each contract's preamble and signature blocks."
          />
        }
      >
        {(data?.items ?? []).map((row) => (
          <PartyRow key={row.key} row={row} />
        ))}
      </RegisterShell>
    </>
  );
}

function PartyRow({ row }: { row: PartyDirectoryEntry }) {
  const currencies = Object.entries(row.total_value);
  return (
    <Card dense>
      <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-semibold text-slate-900 dark:text-slate-100">{row.name}</span>
            {row.is_primary_anywhere ? <Badge text="Signatory" variant="info" /> : null}
            {row.entity_types.map((type) => (
              <Badge key={type} text={humanise(type)} variant="neutral" />
            ))}
          </div>
          <div className="mt-1.5 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-slate-500">
            <span className="font-medium text-slate-700 dark:text-slate-200">
              {row.contract_count} {row.contract_count === 1 ? 'contract' : 'contracts'}
            </span>
            {row.roles.length ? <span>{row.roles.map(humanise).join(', ')}</span> : null}
            {row.jurisdictions.length ? <span>{row.jurisdictions.join(', ')}</span> : null}
            {row.next_expiry ? <span>Next expiry {formatDate(row.next_expiry)}</span> : null}
            {row.sample_contract_id ? (
              <Link
                to={`/contracts/${row.sample_contract_id}`}
                className="font-medium text-blue-600 hover:text-blue-700"
              >
                Open a contract
              </Link>
            ) : null}
          </div>
        </div>

        {/* Reported per currency and never summed: adding 40,000 GBP to 50,000 USD
            gives a number that is wrong in both. */}
        {currencies.length ? (
          <div className="shrink-0 text-right">
            {currencies.map(([currency, value]) => (
              <p
                key={currency}
                className="text-sm font-semibold tabular-nums text-slate-900 dark:text-slate-100"
              >
                {formatMoney(value, currency)}
              </p>
            ))}
            <p className="text-[11px] text-slate-400">total contract value</p>
          </div>
        ) : null}
      </div>
    </Card>
  );
}

export default PortfolioPage;
