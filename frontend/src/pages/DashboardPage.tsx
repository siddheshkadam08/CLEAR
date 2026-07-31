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
  Pie,
  PieChart,
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

const PIE_COLORS = ['#2563EB', '#10B981', '#D97706', '#94A0B4', '#8B5CF6', '#F43F5E', '#06B6D4', '#F59E0B'];

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
            {/* <Card>
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
                <p className="rounded-lg border border-dashed border-[#E4E7EC] px-4 py-10 text-center text-[13px] text-[#5B6478]">
                  Nothing scored yet. Risk appears once extraction finishes.
                </p>
              )}
            </Card> */}

            <Card>
              <SectionHeader
                title="Agreement types"
                subtitle="What kinds of contract the repository holds."
                icon={FileText}
              />
              {data.agreement_type_distribution.length ? (
                <div className="flex items-center gap-6">
                  <div className="h-[180px] w-[180px] shrink-0">
                    <ResponsiveContainer width="100%" height="100%">
                      <PieChart>
                        <Pie
                          data={data.agreement_type_distribution.slice(0, 8)}
                          dataKey="value"
                          nameKey="label"
                          cx="50%"
                          cy="50%"
                          innerRadius={42}
                          outerRadius={80}
                          strokeWidth={0}
                          paddingAngle={2}
                        >
                          {data.agreement_type_distribution.slice(0, 8).map((bucket, i) => (
                            <Cell key={bucket.label} fill={PIE_COLORS[i % PIE_COLORS.length]} />
                          ))}
                        </Pie>
                        <Tooltip
                          contentStyle={{
                            background: '#fff',
                            border: '1px solid #E4E7EC',
                            borderRadius: 8,
                            fontSize: 12,
                          }}
                          formatter={(val) => [String(val), 'Contracts']}
                          labelFormatter={(label) => humanise(String(label))}
                        />
                      </PieChart>
                    </ResponsiveContainer>
                  </div>
                  <div className="min-w-0 flex-1">
                    {data.agreement_type_distribution.slice(0, 6).map((bucket, i) => (
                      <div key={bucket.label} className="flex items-center justify-between border-b border-[#E4E7EC] py-[5px] text-[12.5px] last:border-b-0">
                        <span className="flex items-center gap-2 truncate text-[#0F172A]">
                          <span
                            className="inline-block h-2 w-2 shrink-0 rounded-full"
                            style={{ background: PIE_COLORS[i % PIE_COLORS.length] }}
                          />
                          <span className="truncate">{humanise(bucket.label.toLocaleUpperCase())}</span>
                        </span>
                        <span className="ml-2 shrink-0 text-[#5B6478]">{bucket.percentage}%</span>
                      </div>
                    ))}
                  </div>
                </div>
              ) : (
                <p className="rounded-lg border border-dashed border-[#E4E7EC] px-4 py-10 text-center text-[13px] text-[#5B6478]">
                  No agreements classified yet.
                </p>
              )}
            </Card>
          </div>

          <div className="grid gap-4 xl:grid-cols-2">
            {/* <Card className="overflow-hidden">
              <SectionHeader
                title="Expiring soon"
                subtitle="The next 90 days."
                icon={CalendarClock}
              />
              {data.expiring_soon.length ? (
                <ul className="divide-y divide-[#E4E7EC]">
                  {data.expiring_soon.slice(0, 8).map((row) => (
                    <li key={row.contract_id}>
                      <button
                        type="button"
                        onClick={() => navigate(`/contracts/${row.contract_id}`)}
                        className="flex w-full items-center justify-between gap-3 py-2.5 text-left transition hover:bg-[#F7F8FA]"
                      >
                        <div className="min-w-0">
                            <p className="truncate text-[13px] font-semibold text-[#0F172A]">
                              {row.title ?? 'Untitled'}
                            </p>
                            <p className="mt-0.5 flex flex-wrap items-center gap-2 text-[11.5px] text-[#94A0B4]">
                            <span>{formatDate(row.expiration_date)}</span> */}
                            {/* Auto-renewal is called out because the notice deadline
                                falls *before* the expiry date - by the time the expiry
                                looks close, the window may already have closed. */}
                            {/* {row.auto_renewal ? (
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
                <p className="rounded-lg border border-dashed border-[#E4E7EC] px-4 py-10 text-center text-[13px] text-[#5B6478]">
                  Nothing expiring in the next 90 days.
                </p>
              )}
            </Card> */}

            {/* <Card className="overflow-hidden">
              <SectionHeader
                title="Highest risk"
                subtitle="Where to look first."
                icon={ShieldAlert}
              />
              {data.top_risks.length ? (
                <ul className="divide-y divide-[#E4E7EC]">
                  {data.top_risks.slice(0, 8).map((row) => (
                    <li key={row.contract_id}>
                      <button
                        type="button"
                        onClick={() => navigate(`/contracts/${row.contract_id}`)}
                        className="flex w-full items-center justify-between gap-3 py-2.5 text-left transition hover:bg-[#F7F8FA]"
                      >
                        <p className="min-w-0 flex-1 truncate text-[13px] font-semibold text-[#0F172A]">
                          {row.title ?? 'Untitled'}
                        </p>
                        <div className="flex shrink-0 items-center gap-2">
                          {row.risk_score !== null && row.risk_score !== undefined ? (
                            <span className="text-[11.5px] tabular-nums text-[#94A0B4]">
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
                <p className="rounded-lg border border-dashed border-[#E4E7EC] px-4 py-10 text-center text-[13px] text-[#5B6478]">
                  No risks identified yet.
                </p>
              )}
            </Card> */}
          </div>
        </>
      )}

      {isLoading ? <LoadingSpinner label="Loading dashboard..." /> : null}
    </div>
  );
}

/** Local tile matching the CLEAR stat-card style (big Manrope number, muted label, small accent icon). */
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
        <p className="text-[12.5px] text-[#5B6478]">{label}</p>
        <p className="mt-2 text-[26px] font-semibold leading-tight text-[#0F172A]">{value}</p>
      </div>
      <div className={['shrink-0 rounded-lg p-2', accent].join(' ')}>
        <Icon className="h-4 w-4 sm:h-5 sm:w-5" />
      </div>
    </div>
  );

  if (onClick) {
    return (
      <button
        type="button"
        onClick={onClick}
        className="rounded-xl border border-[#E4E7EC] bg-white p-[18px] text-left shadow-sm transition hover:border-blue-200 hover:shadow-md"
      >
        {body}
      </button>
    );
  }
  return (
    <div className="rounded-xl border border-[#E4E7EC] bg-white p-[18px] shadow-sm">
      {body}
    </div>
  );
}

export default DashboardPage;
