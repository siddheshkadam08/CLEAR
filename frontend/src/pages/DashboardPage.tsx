/**
 * Overview dashboard.
 *
 * The KPI tiles are clickable: each carries a `drilldown` filter from the backend,
 * so "12 expiring" navigates to exactly those twelve rather than leaving the user
 * to reconstruct the filter by hand. A number you cannot act on is decoration.
 */

import { useQuery } from '@tanstack/react-query';
import {
  AlertTriangle,
  BarChart3,
  CalendarClock,
  FileText,
  LayoutDashboard,
  ShieldAlert,
  Upload,
} from 'lucide-react';
import { useNavigate } from 'react-router-dom';
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';

import { dashboard as dashboardApi } from '@/api/endpoints';
import { errorMessage } from '@/api/errors';
import type { KpiTile } from '@/api/types';
import { Badge } from '@/components/common/Badge';
import { getRiskVariant } from '@/lib/badges';
import { ErrorBanner } from '@/components/common/Banner';
import { Button } from '@/components/common/Button';
import {
  ACCENTS,
  Card,
  KpiSkeleton,
  PageHeader,
  SectionHeader,
} from '@/components/common/Card';
import { EmptyState } from '@/components/common/EmptyState';
import { LoadingSpinner } from '@/components/common/LoadingSpinner';
import { formatDate, formatNumber, humanise } from '@/lib/format';
import { useAuth } from '@/lib/auth';
import { useProjectScope } from '@/lib/scope';

/** Chart fills. Literal hex rather than a CSS variable: recharts renders to SVG
 *  attributes and does not resolve `var()` in every browser it supports. */
const RISK_FILLS: Record<string, string> = {
  high: '#e11d48',
  critical: '#be123c',
  medium: '#f59e0b',
  low: '#10b981',
};

const KPI_ICONS = [FileText, CalendarClock, ShieldAlert, AlertTriangle, BarChart3];

export function DashboardPage() {
  const { projectId } = useProjectScope();
  const { user } = useAuth();
  const navigate = useNavigate();
  const isAdmin = Boolean(user?.is_system_admin);

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ['dashboard', projectId],
    queryFn: () => dashboardApi.overview(projectId),
  });

  function openDrilldown(kpi: KpiTile) {
    if (!kpi.drilldown) return;
    const params = new URLSearchParams();
    for (const [key, value] of Object.entries(kpi.drilldown)) {
      if (value !== null && value !== undefined) params.set(key, String(value));
    }
    navigate(`/contracts?${params.toString()}`);
  }

  const noData = data ? data.kpis.every((kpi) => kpi.value === 0) : false;

  return (
    <div className="space-y-5">
      <PageHeader
        title="Overview"
        subtitle={
          projectId
            ? 'This project'
            : data
              ? `Across ${data.project_ids.length} project${data.project_ids.length === 1 ? '' : 's'} you can see`
              : 'Your contract repository at a glance'
        }
      />

      {error ? (
        <ErrorBanner message={errorMessage(error)} onRetry={() => void refetch()} />
      ) : null}

      {isLoading ? (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
          {[0, 1, 2, 3].map((key) => (
            <KpiSkeleton key={key} />
          ))}
        </div>
      ) : !data ? null : noData ? (
        <EmptyState
          icon={LayoutDashboard}
          title="No contracts yet"
          description={
            isAdmin
              ? 'Nothing has been uploaded to the projects you can see. Create a project and add members — they upload the contracts, and this dashboard fills in as documents finish processing.'
              : 'Upload a contract to start building the repository. Extraction runs automatically and this dashboard fills in as documents finish processing.'
          }
          action={
            isAdmin ? (
              <Button onClick={() => navigate('/admin/projects')}>Manage projects</Button>
            ) : (
              <Button icon={Upload} onClick={() => navigate('/upload')}>
                Upload a contract
              </Button>
            )
          }
        />
      ) : (
        <>
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
            {data.kpis.map((kpi, index) => {
              const Icon = KPI_ICONS[index % KPI_ICONS.length] ?? FileText;
              const accent = ACCENTS[index % ACCENTS.length] ?? ACCENTS[0];
              return (
                <MetricTile
                  key={kpi.key}
                  label={kpi.label}
                  value={
                    <>
                      {formatNumber(kpi.unit ? Math.round(kpi.value) : kpi.value)}
                      {kpi.unit ? (
                        <span className="ml-1.5 text-base font-medium text-slate-500">
                          {kpi.unit}
                        </span>
                      ) : null}
                    </>
                  }
                  icon={Icon}
                  accent={accent}
                  onClick={kpi.drilldown ? () => openDrilldown(kpi) : undefined}
                />
              );
            })}
          </div>

          <div className="grid gap-4 xl:grid-cols-2">
            <Card>
              <SectionHeader
                title="Risk distribution"
                subtitle="How the repository scores overall."
                icon={BarChart3}
              />
              {data.risk_distribution.length ? (
                <div className="h-56 w-full sm:h-64">
                  <ResponsiveContainer width="100%" height="100%">
                    <BarChart data={data.risk_distribution}>
                      <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" vertical={false} />
                      <XAxis
                        dataKey="label"
                        tick={{ fontSize: 12, fill: '#64748b' }}
                        tickLine={false}
                        axisLine={{ stroke: '#e2e8f0' }}
                        tickFormatter={humanise}
                      />
                      <YAxis
                        allowDecimals={false}
                        tick={{ fontSize: 12, fill: '#64748b' }}
                        tickLine={false}
                        axisLine={false}
                        width={32}
                      />
                      <Tooltip
                        cursor={{ fill: '#f1f5f9' }}
                        contentStyle={{
                          background: '#ffffff',
                          border: '1px solid #e2e8f0',
                          borderRadius: 12,
                          fontSize: 12,
                          boxShadow: '0 10px 30px -12px rgb(15 23 42 / 0.25)',
                        }}
                        labelFormatter={(label) => humanise(String(label))}
                      />
                      <Bar dataKey="value" radius={[8, 8, 0, 0]} maxBarSize={72}>
                        {data.risk_distribution.map((bucket) => (
                          <Cell
                            key={bucket.label}
                            fill={RISK_FILLS[bucket.label.toLowerCase()] ?? '#2563eb'}
                          />
                        ))}
                      </Bar>
                    </BarChart>
                  </ResponsiveContainer>
                </div>
              ) : (
                <p className="rounded-2xl border border-dashed border-slate-300 px-4 py-10 text-center text-sm text-slate-500">
                  Nothing scored yet. Risk appears once extraction finishes.
                </p>
              )}
            </Card>

            <Card>
              <SectionHeader
                title="Agreement types"
                subtitle="What kinds of contract the repository holds."
                icon={FileText}
              />
              {data.agreement_type_distribution.length ? (
                <ul className="space-y-3">
                  {data.agreement_type_distribution.slice(0, 8).map((bucket) => (
                    <li key={bucket.label} className="flex items-center gap-3">
                      <span className="w-32 shrink-0 truncate text-sm text-slate-700 sm:w-44">
                        {humanise(bucket.label)}
                      </span>
                      <div className="h-2 flex-1 overflow-hidden rounded-full bg-slate-100">
                        <div
                          className="h-full rounded-full bg-blue-600"
                          style={{ width: `${Math.max(bucket.percentage, 2)}%` }}
                        />
                      </div>
                      <span className="w-8 shrink-0 text-right text-xs tabular-nums text-slate-500">
                        {bucket.value}
                      </span>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="rounded-2xl border border-dashed border-slate-300 px-4 py-10 text-center text-sm text-slate-500">
                  No agreements classified yet.
                </p>
              )}
            </Card>
          </div>

          <div className="grid gap-4 xl:grid-cols-2">
            <Card className="overflow-hidden">
              <SectionHeader
                title="Expiring soon"
                subtitle="The next 90 days."
                icon={CalendarClock}
              />
              {data.expiring_soon.length ? (
                <ul className="divide-y divide-slate-100">
                  {data.expiring_soon.slice(0, 8).map((row) => (
                    <li key={row.contract_id}>
                      <button
                        type="button"
                        onClick={() => navigate(`/contracts/${row.contract_id}`)}
                        className="flex w-full items-center justify-between gap-3 py-3 text-left transition hover:bg-slate-50"
                      >
                        <div className="min-w-0">
                          <p className="truncate text-sm font-medium text-slate-900">
                            {row.title ?? 'Untitled'}
                          </p>
                          <p className="mt-0.5 flex flex-wrap items-center gap-2 text-xs text-slate-500">
                            <span>{formatDate(row.expiration_date)}</span>
                            {/* Auto-renewal is called out because the notice deadline
                                falls *before* the expiry date - by the time the expiry
                                looks close, the window may already have closed. */}
                            {row.auto_renewal ? (
                              <Badge text="Auto-renews" variant="warning" />
                            ) : null}
                          </p>
                        </div>
                        <Badge
                          text={
                            row.days_remaining === null || row.days_remaining === undefined
                              ? '—'
                              : `${row.days_remaining}d`
                          }
                          variant={(row.days_remaining ?? 999) <= 30 ? 'danger' : 'neutral'}
                        />
                      </button>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="rounded-2xl border border-dashed border-slate-300 px-4 py-10 text-center text-sm text-slate-500">
                  Nothing expiring in the next 90 days.
                </p>
              )}
            </Card>

            <Card className="overflow-hidden">
              <SectionHeader
                title="Highest risk"
                subtitle="Where to look first."
                icon={ShieldAlert}
              />
              {data.top_risks.length ? (
                <ul className="divide-y divide-slate-100">
                  {data.top_risks.slice(0, 8).map((row) => (
                    <li key={row.contract_id}>
                      <button
                        type="button"
                        onClick={() => navigate(`/contracts/${row.contract_id}`)}
                        className="flex w-full items-center justify-between gap-3 py-3 text-left transition hover:bg-slate-50"
                      >
                        <p className="min-w-0 flex-1 truncate text-sm font-medium text-slate-900">
                          {row.title ?? 'Untitled'}
                        </p>
                        <div className="flex shrink-0 items-center gap-2">
                          {row.risk_score !== null && row.risk_score !== undefined ? (
                            <span className="text-xs tabular-nums text-slate-500">
                              {Math.round(row.risk_score)}
                            </span>
                          ) : null}
                          <Badge
                            text={humanise(row.risk_band) || '—'}
                            variant={getRiskVariant(row.risk_band)}
                          />
                        </div>
                      </button>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="rounded-2xl border border-dashed border-slate-300 px-4 py-10 text-center text-sm text-slate-500">
                  No risks identified yet.
                </p>
              )}
            </Card>
          </div>
        </>
      )}

      {isLoading ? <LoadingSpinner label="Loading dashboard..." /> : null}
    </div>
  );
}

/** Local tile so the value can be a node (number plus unit) rather than a string. */
function MetricTile({
  label,
  value,
  icon: Icon,
  accent,
  onClick,
}: {
  label: string;
  value: React.ReactNode;
  icon: typeof FileText;
  accent: string;
  onClick?: () => void;
}) {
  const body = (
    <div className="flex items-start justify-between gap-3">
      <div className="min-w-0">
        <p className="truncate text-sm font-medium text-slate-500">{label}</p>
        <p className="mt-3 text-2xl font-semibold text-slate-900 sm:text-3xl">{value}</p>
      </div>
      <div className={['shrink-0 rounded-2xl p-3', accent].join(' ')}>
        <Icon className="h-5 w-5 sm:h-6 sm:w-6" />
      </div>
    </div>
  );

  if (onClick) {
    return (
      <button
        type="button"
        onClick={onClick}
        className="rounded-2xl border border-slate-200 bg-white p-5 text-left shadow-sm transition hover:border-blue-200 hover:shadow-md sm:p-6"
      >
        {body}
      </button>
    );
  }
  return (
    <div className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm sm:p-6">
      {body}
    </div>
  );
}

export default DashboardPage;
