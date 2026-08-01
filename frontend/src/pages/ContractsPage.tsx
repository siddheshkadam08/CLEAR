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
  ChevronDown,
  ChevronUp,
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
import { Pagination } from '@/components/common/Pagination';
import { useAuth } from '@/lib/auth';
import { daysUntil, formatDate, formatDateTimeFull, formatMoney, formatNumber, humanise } from '@/lib/format';
import { useProjectScope } from '@/lib/scope';

const PAGE_SIZE = 10;

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

  const [searchInput, setSearchInput] = useState('');

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

  function goToPage(page: number) {
    const next = new URLSearchParams(params);
    next.set('page', String(page));
    setParams(next);
  }

  function toggleSort(field: string) {
    update((next) => {
      const currentBy = next.get('sort_by');
      const currentDir = next.get('sort_dir') ?? 'desc';
      if (currentBy === field) {
        next.set('sort_dir', currentDir === 'asc' ? 'desc' : 'asc');
      } else {
        next.set('sort_by', field);
        next.set('sort_dir', 'asc');
      }
    });
  }

  const activeFilterCount =
    (filters.status?.length ?? 0) +
    (filters.risk_band?.length ?? 0) +
    (filters.q ? 1 : 0) +
    (filters.needs_review ? 1 : 0) +
    (filters.has_unlimited_liability ? 1 : 0) +
    (filters.expiring_before ? 1 : 0);

  const needle = searchInput.trim().toLowerCase();
  const filteredItems = needle && data?.items
    ? data.items.filter((c) =>
        [
          c.title, c.original_file_name, c.party_a, c.party_b, c.vendor,
          c.agreement_type, c.status, formatStatusLabel(c.status),
          c.risk_band, c.currency,
          c.contract_value?.toString(),
          c.expiration_date,
          formatDate(c.expiration_date),
        ].some((v) => typeof v === 'string' && v.toLowerCase().includes(needle))
      )
    : (data?.items ?? []);

  return (
    <div className="space-y-5">
      <PageHeader
        // title="Contracts"
        subtitle={activeFilterCount ? `${activeFilterCount} filter${activeFilterCount > 1 ? 's' : ''} active` : 'Your contract repository'}
        title=""
        // subtitle=""
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

      {/* Stats strip */}
      {data && (
        <div className="flex flex-wrap items-center gap-3 rounded-xl border border-[#E4E7EC] bg-white px-4 py-3 dark:border-slate-700 dark:bg-slate-800">
          <div className="flex items-center gap-2">
            <span className="font-display text-[22px] font-bold leading-none text-slate-800 dark:text-slate-100">{formatNumber(data.meta.total)}</span>
            <span className="text-sm text-slate-500 dark:text-slate-400">contracts</span>
          </div>
          {activeFilterCount > 0 && (
            <>
              <div className="h-5 w-px bg-slate-200 dark:bg-slate-600" />
              <span className="rounded-full bg-blue-50 px-2.5 py-0.5 text-xs font-semibold text-blue-600 dark:bg-blue-950 dark:text-blue-400">
                {activeFilterCount} filter{activeFilterCount > 1 ? 's' : ''} active
              </span>
              <span className="text-xs text-slate-400 dark:text-slate-500">
                {data.items.length < data.meta.total ? `showing ${data.items.length} of ${formatNumber(data.meta.total)}` : `all ${formatNumber(data.meta.total)} shown`}
              </span>
              <button
                type="button"
                onClick={() => setParams({})}
                className="ml-auto text-xs font-medium text-rose-500 transition hover:text-rose-600 dark:text-rose-400 dark:hover:text-rose-300"
              >
                Clear all
              </button>
            </>
          )}
        </div>
      )}

      <Card dense className={filtersOpen ? '' : 'hidden lg:block'}>
        <div className="space-y-3">
          {/* Search + sort on one row */}
          <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
            <input
              type="search"
              placeholder="Search title, party or file name"
              aria-label="Search contracts"
              value={searchInput}
              onChange={(e) => setSearchInput(e.target.value)}
              className={`${inputClasses} sm:flex-1`}
            />
            {/* <div className="relative sm:w-52">
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
                className="h-9 w-full cursor-pointer appearance-none rounded-lg border border-[#E4E7EC] bg-white py-0 pl-3 pr-8 text-[13px] font-medium text-[#0F172A] outline-none transition hover:border-[#94A0B4] focus:border-[#2563EB] focus:ring-2 focus:ring-blue-100 dark:border-slate-600 dark:bg-slate-800 dark:text-slate-100 dark:hover:border-slate-500"
              >
                <option value="created_at:desc">Newest first</option>
                <option value="created_at:asc">Oldest first</option>
                <option value="risk_score:desc">Highest risk</option>
                <option value="expiration_date:asc">Expiring soonest</option>
                <option value="title:asc">Title A–Z</option>
              </select>
              <svg className="pointer-events-none absolute right-2.5 top-1/2 -translate-y-1/2 text-[#5B6478]" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><path d="M6 9l6 6 6-6" /></svg>
            </div> */}
          </div>

          {/* Status + risk chips combined */}
          {/* <div className="flex flex-wrap items-center gap-2">
            <span className="font-mono text-[10px] font-semibold uppercase tracking-widest text-slate-400 dark:text-slate-500">Status</span>
            {STATUSES.map((status) => (
              <Chip
                key={status}
                label={humanise(status)}
                active={Boolean(filters.status?.includes(status))}
                onClick={() => toggleMulti('status', status)}
              />
            ))}
            <div className="mx-1 h-4 w-px bg-slate-200 dark:bg-slate-600" />
            <span className="font-mono text-[10px] font-semibold uppercase tracking-widest text-slate-400 dark:text-slate-500">Risk</span>
            {RISK_BANDS.map((band) => (
              <Chip
                key={band}
                label={`${humanise(band)} risk`}
                active={Boolean(filters.risk_band?.includes(band))}
                onClick={() => toggleMulti('risk_band', band)}
              />
            ))}
          </div> */}

          {/* Toggles */}
          <div className="flex flex-wrap items-center gap-4">
            {/* <Toggle
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
            /> */}
            {activeFilterCount > 0 ? (
              <Button
                variant="ghost"
                size="sm"
                icon={X}
                onClick={() => setParams({})}
                className="ml-auto lg:hidden"
              >
                Clear all ({activeFilterCount})
              </Button>
            ) : null}
          </div>
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
                <thead className="bg-slate-50/80 text-xs dark:bg-slate-800/80">
                  <tr className="border-b border-slate-200 dark:border-slate-700">
                    <th className="px-5 py-3.5 font-semibold uppercase tracking-[0.06em] text-slate-500 dark:text-slate-400">
                      <button type="button" onClick={() => toggleSort('title')} className="inline-flex items-center gap-1 hover:text-[#0F172A] dark:hover:text-slate-100 transition">
                        Contract
                        {filters.sort_by === 'title' ? (filters.sort_dir === 'asc' ? <ChevronUp className="h-3.5 w-3.5" /> : <ChevronDown className="h-3.5 w-3.5" />) : <ChevronDown className="h-3.5 w-3.5 opacity-30" />}
                      </button>
                    </th>
                    <th className="px-5 py-3.5 font-semibold uppercase tracking-[0.06em] text-slate-500 dark:text-slate-400">
                      <button type="button" onClick={() => toggleSort('agreement_type')} className="inline-flex items-center gap-1 hover:text-[#0F172A] dark:hover:text-slate-100 transition">
                        Type
                        {filters.sort_by === 'agreement_type' ? (filters.sort_dir === 'asc' ? <ChevronUp className="h-3.5 w-3.5" /> : <ChevronDown className="h-3.5 w-3.5" />) : <ChevronDown className="h-3.5 w-3.5 opacity-30" />}
                      </button>
                    </th>
                    <th className="px-5 py-3.5 font-semibold uppercase tracking-[0.06em] text-slate-500 dark:text-slate-400">
                      <button type="button" onClick={() => toggleSort('status')} className="inline-flex items-center gap-1 hover:text-[#0F172A] dark:hover:text-slate-100 transition">
                        Status
                        {filters.sort_by === 'status' ? (filters.sort_dir === 'asc' ? <ChevronUp className="h-3.5 w-3.5" /> : <ChevronDown className="h-3.5 w-3.5" />) : <ChevronDown className="h-3.5 w-3.5 opacity-30" />}
                      </button>
                    </th>
                    <th className="px-5 py-3.5 font-semibold uppercase tracking-[0.06em] text-slate-500 dark:text-slate-400">
                      <button type="button" onClick={() => toggleSort('risk_score')} className="inline-flex items-center gap-1 hover:text-[#0F172A] dark:hover:text-slate-100 transition">
                        Risk
                        {filters.sort_by === 'risk_score' ? (filters.sort_dir === 'asc' ? <ChevronUp className="h-3.5 w-3.5" /> : <ChevronDown className="h-3.5 w-3.5" />) : <ChevronDown className="h-3.5 w-3.5 opacity-30" />}
                      </button>
                    </th>
                    {/* <th className="px-5 py-3.5 text-right font-semibold uppercase tracking-[0.06em] text-slate-500 dark:text-slate-400">
                      <button
                        type="button"
                        onClick={() => toggleSort('contract_value')}
                        className="inline-flex items-center gap-1 hover:text-[#0F172A] dark:hover:text-slate-100 transition"
                      >
                        Value
                        {filters.sort_by === 'contract_value' ? (
                          filters.sort_dir === 'asc' ? <ChevronUp className="h-3.5 w-3.5" /> : <ChevronDown className="h-3.5 w-3.5" />
                        ) : (
                          <ChevronDown className="h-3.5 w-3.5 opacity-30" />
                        )}
                      </button>
                    </th> */}
                    <th className="px-5 py-3.5 font-semibold uppercase tracking-[0.06em] text-slate-500 dark:text-slate-400">
                      <button
                        type="button"
                        onClick={() => toggleSort('created_at')}
                        className="inline-flex items-center gap-1 hover:text-[#0F172A] dark:hover:text-slate-100 transition"
                      >
                        Created On
                        {filters.sort_by === 'created_at' ? (
                          filters.sort_dir === 'asc' ? <ChevronUp className="h-3.5 w-3.5" /> : <ChevronDown className="h-3.5 w-3.5" />
                        ) : (
                          <ChevronDown className="h-3.5 w-3.5 opacity-30" />
                        )}
                      </button>
                    </th>
                    <th className="px-5 py-3.5 font-semibold uppercase tracking-[0.06em] text-slate-500 dark:text-slate-400">
                      <button
                        type="button"
                        onClick={() => toggleSort('expiration_date')}
                        className="inline-flex items-center gap-1 hover:text-[#0F172A] dark:hover:text-slate-100 transition"
                      >
                        Expires
                        {filters.sort_by === 'expiration_date' ? (
                          filters.sort_dir === 'asc' ? <ChevronUp className="h-3.5 w-3.5" /> : <ChevronDown className="h-3.5 w-3.5" />
                        ) : (
                          <ChevronDown className="h-3.5 w-3.5 opacity-30" />
                        )}
                      </button>
                    </th>
                    <th className="px-5 py-3.5 font-semibold uppercase tracking-[0.06em] text-slate-500 dark:text-slate-400">
                      Uploaded By
                    </th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100/80 dark:divide-slate-700/50">
                  {filteredItems.map((contract) => (
                    <ContractRow
                      key={contract.id}
                      contract={contract}
                      onOpen={() => navigate(`/contracts/${contract.id}`)}
                    />
                  ))}
                </tbody>
              </table>
            </div>
            {data.meta.pages > 1 && (
              <div className="border-t border-slate-200 bg-slate-50/80 px-5 py-3 dark:border-slate-700 dark:bg-slate-800/80">
                <Pagination
                  page={data.meta.page}
                  pages={data.meta.pages}
                  total={data.meta.total}
                  pageSize={PAGE_SIZE}
                  onPage={goToPage}
                />
              </div>
            )}
          </Card>

          <div className="space-y-3 lg:hidden">
            {filteredItems.map((contract) => (
              <ContractCard
                key={contract.id}
                contract={contract}
                onOpen={() => navigate(`/contracts/${contract.id}`)}
              />
            ))}
          </div>

          {data.meta.pages > 1 && (
            <div className="rounded-xl border border-slate-200 bg-white px-5 py-3 lg:hidden dark:border-slate-700 dark:bg-slate-800">
              <Pagination
                page={data.meta.page}
                pages={data.meta.pages}
                total={data.meta.total}
                pageSize={PAGE_SIZE}
                onPage={goToPage}
              />
            </div>
          )}
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

/** Returns the Tailwind bg-color class for a contract's risk band. */
function riskBar(band: string | null | undefined): string {
  if (band === 'critical' || band === 'high') return 'bg-rose-500';
  if (band === 'medium') return 'bg-amber-400';
  if (band === 'low') return 'bg-emerald-400';
  return 'bg-slate-200 dark:bg-slate-600';
}

function ContractRow({ contract, onOpen }: { contract: ContractListItem; onOpen: () => void }) {
  const { expiringSoon, expired } = useExpiry(contract);

  return (
    <tr onClick={onOpen} className="cursor-pointer transition hover:bg-blue-50/40 dark:hover:bg-blue-950/10">
      <td className="max-w-xs px-5 py-3">
        <p className="truncate font-medium text-slate-900 dark:text-slate-100">
          {contract.title ?? contract.original_file_name}
        </p>
        <p className="truncate text-xs text-slate-500 dark:text-slate-400">{contract.original_file_name}</p>
        {contract.status === 'processing' && contract.processing ? (
          <div className="mt-2">
            <div className="h-1.5 w-full overflow-hidden rounded-full bg-slate-100 dark:bg-slate-700">
              <div
                className="h-full rounded-full bg-blue-600 transition-all"
                style={{ width: `${contract.processing.progress}%` }}
              />
            </div>
            <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">
              {humanise(contract.processing.current_stage)}
            </p>
          </div>
        ) : null}
      </td>
      <td className="px-5 py-3 text-slate-600 uppercase dark:text-slate-400">{humanise(contract.agreement_type)}</td>
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
      {/* <td className="px-5 py-3 text-right tabular-nums text-slate-700 dark:text-slate-300">
        {formatMoney(contract.contract_value, contract.currency)}
      </td> */}
      <td className="px-5 py-3 text-slate-600 dark:text-slate-300">
        {formatDateTimeFull(contract.created_at)}
      </td>
      <td className="px-5 py-3">
        <span
          className={
            expired ? 'text-rose-600 dark:text-rose-400' : expiringSoon ? 'text-amber-600 dark:text-amber-400' : 'text-slate-600 dark:text-slate-300'
          }
        >
          {formatDate(contract.expiration_date)}
        </span>
      </td>
      <td className="px-5 py-3 text-slate-600 dark:text-slate-300">
        {(contract.uploaded_by as { full_name?: string; email?: string } | null)?.full_name
          ?? (contract.uploaded_by as { email?: string } | null)?.email
          ?? '—'}
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
      className="w-full overflow-hidden rounded-2xl border border-slate-200 bg-white text-left shadow-sm transition hover:border-blue-200 hover:shadow-md dark:border-slate-700 dark:bg-slate-800 dark:hover:border-blue-800"
    >
      <div className={`h-1 w-full ${riskBar(contract.risk_band)}`} />
      <div className="p-4">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="truncate font-semibold text-slate-900 dark:text-slate-100">
            {contract.title ?? contract.original_file_name}
          </p>
          <p className="truncate text-xs text-slate-500 dark:text-slate-400">{contract.original_file_name}</p>
        </div>
        <Badge
          text={formatStatusLabel(contract.status)}
          variant={getStatusVariant(contract.status)}
        />
      </div>

      {contract.status === 'processing' && contract.processing ? (
        <div className="mt-3">
          <div className="h-1.5 w-full overflow-hidden rounded-full bg-slate-100 dark:bg-slate-700">
            <div
              className="h-full rounded-full bg-blue-600 transition-all"
              style={{ width: `${contract.processing.progress}%` }}
            />
          </div>
          <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">
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
        <span className="text-xs text-slate-500 dark:text-slate-400">{humanise(contract.agreement_type)}</span>
      </div>

      <dl className="mt-3 grid grid-cols-2 gap-3 border-t border-slate-100 pt-3 text-xs dark:border-slate-700">
        <div>
          <dt className="text-slate-500 dark:text-slate-400">Value</dt>
          <dd className="mt-0.5 font-medium text-slate-900 dark:text-slate-100">
            {formatMoney(contract.contract_value, contract.currency)}
          </dd>
        </div>
        <div>
          <dt className="text-slate-500 dark:text-slate-400">Expires</dt>
          <dd
            className={[
              'mt-0.5 font-medium',
              expired ? 'text-rose-600' : expiringSoon ? 'text-amber-600' : 'text-slate-900',
            ].join(' ')}
          >
            {formatDate(contract.expiration_date)}
          </dd>
        </div>
      </dl>      </div>    </button>
  );
}

export default ContractsPage;
