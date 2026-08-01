/**
 * Contract repository.
 *
 * Filters live in the URL, not in component state, so a filtered view is a link a
 * reviewer can send to a colleague and the browser's back button works. The
 * dashboard's KPI drilldowns land here with those same query parameters.
 *
 * Table above `lg`, cards below it. The row carries eight columns; on a phone that
 * is a horizontal scroll nobody finds, so the same data is restacked rather than
 * shrunk.
 */

import { keepPreviousData, useQuery } from '@tanstack/react-query';
import {
  ChevronLeft,
  ChevronRight,
  FileText,
  SlidersHorizontal,
  Upload,
  X,
} from 'lucide-react';
import { useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';

import { contracts as contractsApi, type ContractFilters } from '@/api/endpoints';
import { errorMessage } from '@/api/errors';
import type { ContractListItem } from '@/api/types';
import { Badge } from '@/components/common/Badge';
import { formatStatusLabel, getRiskVariant, getStatusVariant } from '@/lib/badges';
import { ErrorBanner } from '@/components/common/Banner';
import { Button } from '@/components/common/Button';
import { Card, PageHeader } from '@/components/common/Card';
import { EmptyState } from '@/components/common/EmptyState';
import { inputClasses } from '@/components/common/Field';
import { LoadingSpinner } from '@/components/common/LoadingSpinner';
import { ExportButton } from '@/components/ExportButton';
import { useAuth } from '@/lib/auth';
import { daysUntil, formatAgreementType, formatDate, formatMoney, humanise } from '@/lib/format';
import { useProjectScope } from '@/lib/scope';

const STATUSES = ['uploaded', 'processing', 'ready', 'needs_review', 'failed', 'archived'];
const RISK_BANDS = ['high', 'medium', 'low'];
const PAGE_SIZE = 25;

export function ContractsPage() {
  const { projectId } = useProjectScope();
  const { user } = useAuth();
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const [filtersOpen, setFiltersOpen] = useState(false);
  const isAdmin = Boolean(user?.is_system_admin);

  const filters: ContractFilters = {
    page: Number(params.get('page') ?? 1),
    size: PAGE_SIZE,
    q: params.get('q') ?? undefined,
    status: params.getAll('status'),
    risk_band: params.getAll('risk_band'),
    agreement_type: params.getAll('agreement_type'),
    needs_review: params.get('needs_review') === 'true' ? true : undefined,
    expiring_before: params.get('expiring_before') ?? undefined,
    has_unlimited_liability:
      params.get('has_unlimited_liability') === 'true' ? true : undefined,
    sort_by: params.get('sort_by') ?? undefined,
    sort_dir: params.get('sort_dir') ?? undefined,
  };

  /**
   * The same filters, in the shape the export endpoint takes.
   *
   * The list endpoint accepts flat query parameters; the export takes a
   * `ContractFilterParams` body, where free text is `search` and dates are
   * ranges. Translating here — rather than hoping the two happen to line up —
   * is what keeps the workbook equal to the view it was launched from.
   */
  const exportFilters: Record<string, unknown> = {};
  if (filters.q) exportFilters.search = filters.q;
  if (filters.status?.length) exportFilters.status = filters.status;
  if (filters.risk_band?.length) exportFilters.risk_band = filters.risk_band;
  if (filters.agreement_type?.length) exportFilters.agreement_type = filters.agreement_type;
  if (filters.needs_review !== undefined) exportFilters.needs_review = filters.needs_review;
  if (filters.has_unlimited_liability !== undefined) {
    exportFilters.has_unlimited_liability = filters.has_unlimited_liability;
  }
  if (filters.expiring_before || filters.expiring_after) {
    exportFilters.expiration_date = {
      from: filters.expiring_after ?? null,
      to: filters.expiring_before ?? null,
    };
  }

  const { data, isLoading, error, refetch, isFetching } = useQuery({
    queryKey: ['contracts', projectId, params.toString()],
    queryFn: () => contractsApi.list(projectId, filters),
    // Keeps the previous page visible while the next one loads, so paging does
    // not blank the table out on every click.
    placeholderData: keepPreviousData,
    // A contract mid-pipeline changes state on its own; without this the row
    // sits on "processing" until the user reloads.
    refetchInterval: (query) =>
      query.state.data?.items.some((item) => item.status === 'processing') ? 5000 : false,
  });

  function update(mutate: (next: URLSearchParams) => void) {
    const next = new URLSearchParams(params);
    mutate(next);
    // Any filter change invalidates the page number - page 4 of the old result
    // set is very unlikely to exist in the new one.
    next.delete('page');
    setParams(next, { replace: true });
  }

  function toggleMulti(key: string, value: string) {
    update((next) => {
      const current = next.getAll(key);
      next.delete(key);
      for (const existing of current) {
        if (existing !== value) next.append(key, existing);
      }
      if (!current.includes(value)) next.append(key, value);
    });
  }

  function goToPage(page: number) {
    const next = new URLSearchParams(params);
    next.set('page', String(page));
    setParams(next);
  }

  const activeFilterCount =
    (filters.status?.length ?? 0) +
    (filters.risk_band?.length ?? 0) +
    (filters.q ? 1 : 0) +
    (filters.needs_review ? 1 : 0) +
    (filters.has_unlimited_liability ? 1 : 0) +
    (filters.expiring_before ? 1 : 0);

  return (
    <div className="space-y-5">
      <PageHeader
        title="Contracts"
        subtitle={data ? `${data.meta.total} in scope` : 'Your contract repository'}
        actions={
          <>
            <Button
              variant="secondary"
              size="sm"
              icon={SlidersHorizontal}
              className="lg:hidden"
              onClick={() => setFiltersOpen((open) => !open)}
            >
              Filters{activeFilterCount ? ` (${activeFilterCount})` : ''}
            </Button>
            {activeFilterCount > 0 ? (
              <Button
                variant="ghost"
                size="sm"
                icon={X}
                onClick={() => setParams({})}
                className="hidden lg:inline-flex"
              >
                Clear ({activeFilterCount})
              </Button>
            ) : null}
            {/* The same filters the table is showing, so the workbook and the
                screen cannot disagree. */}
            <ExportButton filters={exportFilters} projectId={projectId} />
            {/* Uploading is project-member work; an administrator has no upload
                permission, so the call to action would only lead to a 403. */}
            {!isAdmin ? (
              <Button size="sm" icon={Upload} onClick={() => navigate('/upload')}>
                Upload
              </Button>
            ) : null}
          </>
        }
      />

      <Card dense className={filtersOpen ? '' : 'hidden lg:block'}>
        <div className="space-y-4">
          <input
            type="search"
            placeholder="Search title, party or file name"
            aria-label="Search contracts"
            defaultValue={filters.q ?? ''}
            onKeyDown={(event) => {
              if (event.key === 'Enter') {
                const value = event.currentTarget.value.trim();
                update((next) => {
                  if (value) next.set('q', value);
                  else next.delete('q');
                });
              }
            }}
            className={`${inputClasses} lg:max-w-sm`}
          />

          <div className="flex flex-wrap gap-2">
            {STATUSES.map((status) => (
              <Chip
                key={status}
                label={humanise(status)}
                active={Boolean(filters.status?.includes(status))}
                onClick={() => toggleMulti('status', status)}
              />
            ))}
          </div>

          <div className="flex flex-wrap gap-2">
            {RISK_BANDS.map((band) => (
              <Chip
                key={band}
                label={`${humanise(band)} risk`}
                active={Boolean(filters.risk_band?.includes(band))}
                onClick={() => toggleMulti('risk_band', band)}
              />
            ))}
          </div>

          <div className="flex flex-col gap-3 sm:flex-row sm:flex-wrap sm:items-center">
            <Toggle
              label="Needs review"
              checked={Boolean(filters.needs_review)}
              onChange={(checked) =>
                update((next) => {
                  if (checked) next.set('needs_review', 'true');
                  else next.delete('needs_review');
                })
              }
            />
            <Toggle
              label="Unlimited liability"
              checked={Boolean(filters.has_unlimited_liability)}
              onChange={(checked) =>
                update((next) => {
                  if (checked) next.set('has_unlimited_liability', 'true');
                  else next.delete('has_unlimited_liability');
                })
              }
            />

            <select
              aria-label="Sort order"
              value={`${filters.sort_by ?? 'created_at'}:${filters.sort_dir ?? 'desc'}`}
              onChange={(event) => {
                const [by = 'created_at', dir = 'desc'] = event.target.value.split(':');
                update((next) => {
                  next.set('sort_by', by);
                  next.set('sort_dir', dir);
                });
              }}
              className={`${inputClasses} sm:ml-auto sm:w-52`}
            >
              <option value="created_at:desc">Newest first</option>
              <option value="created_at:asc">Oldest first</option>
              <option value="risk_score:desc">Highest risk</option>
              <option value="expiration_date:asc">Expiring soonest</option>
              <option value="title:asc">Title A–Z</option>
            </select>
          </div>

          {activeFilterCount > 0 ? (
            <Button
              variant="ghost"
              size="sm"
              icon={X}
              onClick={() => setParams({})}
              className="lg:hidden"
            >
              Clear all filters ({activeFilterCount})
            </Button>
          ) : null}
        </div>
      </Card>

      {error ? (
        <ErrorBanner message={errorMessage(error)} onRetry={() => void refetch()} />
      ) : null}

      {isLoading ? (
        <Card>
          <LoadingSpinner label="Loading contracts..." />
        </Card>
      ) : data?.items.length ? (
        <div className={isFetching ? 'opacity-70 transition-opacity' : 'transition-opacity'}>
          <Card className="hidden overflow-hidden p-0 lg:block">
            <div className="overflow-x-auto">
              <table className="w-full text-left text-sm">
                <thead className="border-b border-slate-200 bg-slate-50 text-xs uppercase tracking-wider text-slate-500">
                  <tr>
                    <th className="px-5 py-3 font-semibold">Contract</th>
                    <th className="px-5 py-3 font-semibold">Type</th>
                    <th className="px-5 py-3 font-semibold">Parties</th>
                    <th className="px-5 py-3 font-semibold">Status</th>
                    <th className="px-5 py-3 font-semibold">Risk</th>
                    <th className="px-5 py-3 text-right font-semibold">Value</th>
                    <th className="px-5 py-3 font-semibold">Expires</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {data.items.map((contract) => (
                    <ContractRow
                      key={contract.id}
                      contract={contract}
                      onOpen={() => navigate(`/contracts/${contract.id}`)}
                    />
                  ))}
                </tbody>
              </table>
            </div>
          </Card>

          <div className="space-y-3 lg:hidden">
            {data.items.map((contract) => (
              <ContractCard
                key={contract.id}
                contract={contract}
                onOpen={() => navigate(`/contracts/${contract.id}`)}
              />
            ))}
          </div>

          {data.meta.pages > 1 ? (
            <div className="mt-4 flex items-center justify-between gap-3">
              <span className="text-sm text-slate-500">
                Page {data.meta.page} of {data.meta.pages}
              </span>
              <div className="flex gap-2">
                <Button
                  variant="secondary"
                  size="sm"
                  icon={ChevronLeft}
                  disabled={!data.meta.has_prev}
                  onClick={() => goToPage(data.meta.page - 1)}
                >
                  Previous
                </Button>
                <Button
                  variant="secondary"
                  size="sm"
                  disabled={!data.meta.has_next}
                  onClick={() => goToPage(data.meta.page + 1)}
                >
                  Next
                  <ChevronRight className="h-4 w-4" />
                </Button>
              </div>
            </div>
          ) : null}
        </div>
      ) : (
        <EmptyState
          icon={FileText}
          title={activeFilterCount ? 'No contracts match these filters' : 'No contracts yet'}
          description={
            activeFilterCount
              ? 'Try removing a filter. Note that the repository only ever shows projects you are a member of.'
              : isAdmin
                ? 'Nothing has been uploaded to the projects you can see. Project members upload contracts; you can add people to a project from the Projects screen.'
                : 'Upload a contract to begin. Processing runs through eight stages and the row updates as it goes.'
          }
          action={
            activeFilterCount ? (
              <Button variant="secondary" onClick={() => setParams({})}>
                Clear filters
              </Button>
            ) : isAdmin ? (
              <Button onClick={() => navigate('/admin/projects')}>Manage projects</Button>
            ) : (
              <Button icon={Upload} onClick={() => navigate('/upload')}>
                Upload a contract
              </Button>
            )
          }
        />
      )}
    </div>
  );
}

// =============================================================================
// Filter controls
// =============================================================================
const Chip = ({
  label,
  active,
  onClick,
}: {
  label: string;
  active: boolean;
  onClick: () => void;
}) => (
  <button
    type="button"
    onClick={onClick}
    aria-pressed={active}
    className={[
      'rounded-full px-3 py-1.5 text-xs font-medium ring-1 ring-inset transition',
      active
        ? 'bg-blue-600 text-white ring-blue-600'
        : 'bg-white text-slate-600 ring-slate-200 hover:bg-slate-50',
    ].join(' ')}
  >
    {label}
  </button>
);

const Toggle = ({
  label,
  checked,
  onChange,
}: {
  label: string;
  checked: boolean;
  onChange: (checked: boolean) => void;
}) => (
  <label className="inline-flex items-center gap-2 text-sm text-slate-700">
    <input
      type="checkbox"
      checked={checked}
      onChange={(event) => onChange(event.target.checked)}
      className="h-4 w-4 rounded border-slate-300 text-blue-600 focus:ring-blue-500"
    />
    {label}
  </label>
);

// =============================================================================
// Rows
// =============================================================================
/** Shared derivation so the table row and the card cannot disagree. */
function useExpiry(contract: ContractListItem) {
  const remaining = daysUntil(contract.expiration_date);
  return {
    expiringSoon: remaining !== null && remaining >= 0 && remaining <= 30,
    expired: remaining !== null && remaining < 0,
  };
}

function ContractRow({ contract, onOpen }: { contract: ContractListItem; onOpen: () => void }) {
  const { expiringSoon, expired } = useExpiry(contract);

  return (
    <tr onClick={onOpen} className="cursor-pointer transition hover:bg-slate-50">
      <td className="max-w-xs px-5 py-3">
        <p className="truncate font-medium text-slate-900">
          {contract.title ?? contract.original_file_name}
        </p>
        <p className="truncate text-xs text-slate-500">{contract.original_file_name}</p>
        {contract.status === 'processing' && contract.processing ? (
          <div className="mt-2">
            <div className="h-1.5 w-full overflow-hidden rounded-full bg-slate-100">
              <div
                className="h-full rounded-full bg-blue-600 transition-all"
                style={{ width: `${contract.processing.progress}%` }}
              />
            </div>
            <p className="mt-1 text-xs text-slate-500">
              {humanise(contract.processing.current_stage)}
            </p>
          </div>
        ) : null}
      </td>
      <td className="px-5 py-3 text-slate-600">{formatAgreementType(contract.agreement_type)}</td>
      <td className="max-w-[12rem] px-5 py-3">
        <p className="truncate text-slate-700">{contract.party_a ?? '—'}</p>
        <p className="truncate text-xs text-slate-500">{contract.party_b ?? '—'}</p>
      </td>
      <td className="px-5 py-3">
        <div className="flex flex-wrap items-center gap-1.5">
          <Badge
            text={formatStatusLabel(contract.status)}
            variant={getStatusVariant(contract.status)}
          />
          {contract.needs_review && contract.status !== 'needs_review' ? (
            <Badge text="Review" variant="warning" />
          ) : null}
        </div>
      </td>
      <td className="px-5 py-3">
        <Badge
          text={contract.risk_band ? humanise(contract.risk_band) : '—'}
          variant={getRiskVariant(contract.risk_band)}
        />
      </td>
      <td className="px-5 py-3 text-right tabular-nums text-slate-700">
        {formatMoney(contract.contract_value, contract.currency)}
      </td>
      <td className="px-5 py-3">
        <span
          className={
            expired ? 'text-rose-600' : expiringSoon ? 'text-amber-600' : 'text-slate-600'
          }
        >
          {formatDate(contract.expiration_date)}
        </span>
      </td>
    </tr>
  );
}

function ContractCard({
  contract,
  onOpen,
}: {
  contract: ContractListItem;
  onOpen: () => void;
}) {
  const { expiringSoon, expired } = useExpiry(contract);

  return (
    <button
      type="button"
      onClick={onOpen}
      className="w-full rounded-2xl border border-slate-200 bg-white p-4 text-left shadow-sm transition hover:border-blue-200 hover:shadow-md"
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="truncate font-semibold text-slate-900">
            {contract.title ?? contract.original_file_name}
          </p>
          <p className="truncate text-xs text-slate-500">{contract.original_file_name}</p>
        </div>
        <Badge
          text={formatStatusLabel(contract.status)}
          variant={getStatusVariant(contract.status)}
        />
      </div>

      {contract.status === 'processing' && contract.processing ? (
        <div className="mt-3">
          <div className="h-1.5 w-full overflow-hidden rounded-full bg-slate-100">
            <div
              className="h-full rounded-full bg-blue-600 transition-all"
              style={{ width: `${contract.processing.progress}%` }}
            />
          </div>
          <p className="mt-1 text-xs text-slate-500">
            {humanise(contract.processing.current_stage)}
          </p>
        </div>
      ) : null}

      <div className="mt-3 flex flex-wrap items-center gap-2">
        <Badge
          text={contract.risk_band ? `${humanise(contract.risk_band)} risk` : 'Unscored'}
          variant={getRiskVariant(contract.risk_band)}
        />
        {contract.needs_review && contract.status !== 'needs_review' ? (
          <Badge text="Review" variant="warning" />
        ) : null}
        <span className="text-xs text-slate-500">{formatAgreementType(contract.agreement_type)}</span>
      </div>

      <dl className="mt-3 grid grid-cols-2 gap-3 border-t border-slate-100 pt-3 text-xs">
        <div>
          <dt className="text-slate-500">Value</dt>
          <dd className="mt-0.5 font-medium text-slate-900">
            {formatMoney(contract.contract_value, contract.currency)}
          </dd>
        </div>
        <div>
          <dt className="text-slate-500">Expires</dt>
          <dd
            className={[
              'mt-0.5 font-medium',
              expired ? 'text-rose-600' : expiringSoon ? 'text-amber-600' : 'text-slate-900',
            ].join(' ')}
          >
            {formatDate(contract.expiration_date)}
          </dd>
        </div>
      </dl>
    </button>
  );
}

export default ContractsPage;
