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
  ArrowRight,
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

import { contracts as contractsApi, dashboard as dashboardApi } from '@/api/endpoints';
import { errorMessage } from '@/api/errors';
import type { KpiTile } from '@/api/types';
import { ErrorBanner } from '@/components/common/Banner';
import { Button } from '@/components/common/Button';
import {
  Card,
  KpiSkeleton,
  SectionHeader,
} from '@/components/common/Card';
import { EmptyState } from '@/components/common/EmptyState';
import { LoadingSpinner } from '@/components/common/LoadingSpinner';
import { formatMoney, formatNumber, humanise } from '@/lib/format';
import { useAuth } from '@/lib/auth';
import { useProjectScope } from '@/lib/scope';
import { useTheme } from '@/lib/theme';

const PIE_COLORS = ['#2563EB', '#10B981', '#D97706', '#94A0B4', '#8B5CF6', '#F43F5E', '#06B6D4', '#F59E0B'];

const STATUS_FILLS: Record<string, string> = {
  ready: '#10b981',
  processing: '#2563eb',
  needs_review: '#f59e0b',
  uploaded: '#94a3b8',
  failed: '#ef4444',
  archived: '#64748b',
};

const RISK_FILLS: Record<string, string> = {
  critical: '#be123c',
  high: '#e11d48',
  medium: '#f59e0b',
  low: '#10b981',
};

function truncName(s: string, max = 22): string {
  return s.length > max ? s.slice(0, max) + '…' : s;
}

function shortValue(v: number): string {
  if (v >= 1_000_000) return `${(v / 1_000_000).toFixed(1)}M`;
  if (v >= 1_000) return `${(v / 1_000).toFixed(0)}K`;
  return String(v);
}

const KPI_ICONS = [FileText, CalendarClock, ShieldAlert, AlertTriangle, BarChart3];

/** Maps a KPI key to its semantic accent and bar color. */
function getKpiColor(key: string): { accent: string; bar: string } {
  const k = key.toLowerCase();
  if (k.includes('expir'))  return { accent: 'bg-amber-50 text-amber-600 dark:bg-amber-950/60 dark:text-amber-400',  bar: 'bg-amber-500'  };
  if (k.includes('risk'))   return { accent: 'bg-rose-50 text-rose-600 dark:bg-rose-950/60 dark:text-rose-400',      bar: 'bg-rose-500'   };
  if (k.includes('liab'))   return { accent: 'bg-orange-50 text-orange-600 dark:bg-orange-950/60 dark:text-orange-400', bar: 'bg-orange-500' };
  if (k.includes('miss') || k.includes('claus')) return { accent: 'bg-violet-50 text-violet-600 dark:bg-violet-950/60 dark:text-violet-400', bar: 'bg-violet-500' };
  if (k.includes('review')) return { accent: 'bg-indigo-50 text-indigo-600 dark:bg-indigo-950/60 dark:text-indigo-400', bar: 'bg-indigo-500' };
  if (k.includes('value'))  return { accent: 'bg-emerald-50 text-emerald-600 dark:bg-emerald-950/60 dark:text-emerald-400', bar: 'bg-emerald-500' };
  return                           { accent: 'bg-blue-50 text-blue-600 dark:bg-blue-950/60 dark:text-blue-400',        bar: 'bg-blue-500'   };
}

const ACTION_KEYWORDS = ['expir', 'risk', 'liab', 'miss', 'claus', 'review'];
function isActionKpi(key: string) {
  const k = key.toLowerCase();
  return ACTION_KEYWORDS.some((kw) => k.includes(kw));
}

export function DashboardPage() {
  const { projectId } = useProjectScope();
  const { user } = useAuth();
  const navigate = useNavigate();
  const isAdmin = Boolean(user?.is_system_admin);

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ['dashboard', projectId],
    queryFn: () => dashboardApi.overview(projectId),
  });

  const topContractsQuery = useQuery({
    queryKey: ['contracts-top-value', projectId],
    queryFn: () => contractsApi.list(projectId, { sort_by: 'contract_value', sort_dir: 'desc', size: 5, page: 1 }),
  });

  const { theme } = useTheme();
  const isDark = theme === 'dark';
  const chartTick = { fontSize: 11, fill: isDark ? '#94a3b8' : '#64748b' };
  const chartGrid = isDark ? '#334155' : '#e2e8f0';
  const tooltipStyle = {
    background: isDark ? '#1e293b' : '#fff',
    border: `1px solid ${isDark ? '#334155' : '#E4E7EC'}`,
    borderRadius: 8,
    fontSize: 12,
    color: isDark ? '#f1f5f9' : '#0f172a',
  };

  const topContracts = (topContractsQuery.data?.items ?? [])
    .filter((c) => (c.contract_value ?? 0) > 0)
    .map((c) => ({
      name: truncName(c.title ?? c.original_file_name ?? 'Untitled'),
      fullName: c.title ?? c.original_file_name ?? 'Untitled',
      value: c.contract_value ?? 0,
      currency: c.currency ?? 'USD',
    }));

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
      {/* <PageHeader
        title="Overview"
        subtitle={
          projectId
            ? 'This project'
            : data
              ? `Across ${data.project_ids.length} project${data.project_ids.length === 1 ? '' : 's'} you can see`
              : 'Your contract repository at a glance'
        }
      /> */}

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
          {/* Attention ribbon — only visible when action KPIs have non-zero values */}
          {data.kpis.some((k) => k.drilldown && k.value > 0 && isActionKpi(k.key)) && (
            <div className="flex flex-wrap items-center gap-2 rounded-xl border border-rose-200/70 bg-rose-50/50 px-4 py-3 dark:border-rose-900/30 dark:bg-rose-950/20">
              <span className="mr-1 shrink-0 font-mono text-[10px] font-semibold uppercase tracking-widest text-rose-500 dark:text-rose-400">
                Needs attention
              </span>
              {data.kpis
                .filter((k) => k.drilldown && k.value > 0 && isActionKpi(k.key))
                .map((kpi) => (
                  <button
                    key={kpi.key}
                    type="button"
                    onClick={() => openDrilldown(kpi)}
                    className="flex items-center gap-2 rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-[12px] shadow-sm transition hover:border-blue-300 hover:shadow dark:border-slate-600 dark:bg-slate-800 dark:hover:border-blue-700"
                  >
                    <span className={['h-2 w-2 shrink-0 rounded-full', getKpiColor(kpi.key).bar].join(' ')} />
                    <span className="font-bold tabular-nums text-slate-800 dark:text-slate-100">{formatNumber(kpi.value)}</span>
                    <span className="font-medium text-slate-500 dark:text-slate-400">{kpi.label}</span>
                  </button>
                ))}
            </div>
          )}

          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
            {data.kpis.filter((k) => !isActionKpi(k.key)).map((kpi, index) => {
              const Icon = KPI_ICONS[index % KPI_ICONS.length] ?? FileText;
              return (
                <MetricTile
                  key={kpi.key}
                  label={kpi.label}
                  value={
                    <>
                      {formatNumber(kpi.unit ? Math.round(kpi.value) : kpi.value)}
                      {kpi.unit ? (
                        <span className="ml-1.5 text-base font-medium text-slate-500 dark:text-slate-400">
                          {kpi.unit}
                        </span>
                      ) : null}
                    </>
                  }
                  icon={Icon}
                  kpiKey={kpi.key}
                  onClick={() => kpi.drilldown ? openDrilldown(kpi) : navigate('/contracts')}
                />
              );
            })}
          </div>

          {/* Row 1: Top 5 by value + Agreement types */}
          <div className="grid gap-4 xl:grid-cols-2">
            <Card>
              <SectionHeader title="Top 5 by value" subtitle="Highest-value contracts in scope." icon={BarChart3} />
              {topContracts.length ? (
                <div className="h-[220px] w-full">
                  <ResponsiveContainer width="100%" height="100%">
                    <BarChart layout="vertical" data={topContracts} margin={{ top: 4, right: 24, bottom: 4, left: 0 }}>
                      <CartesianGrid strokeDasharray="3 3" stroke={chartGrid} horizontal={false} />
                      <XAxis type="number" tick={chartTick} tickLine={false} axisLine={false} tickFormatter={shortValue} />
                      <YAxis type="category" dataKey="name" tick={chartTick} tickLine={false} axisLine={false} width={120} />
                      <Tooltip
                        contentStyle={tooltipStyle}
                        formatter={(val, _name, item) => {
                          // `item` is optional on Recharts' formatter signature and
                          // is genuinely absent on some render paths, so the
                          // shorter `item.payload` form throws rather than
                          // rendering an empty tooltip.
                          const row = item?.payload as
                            | { fullName?: string; currency?: string }
                            | undefined;
                          return [formatMoney(Number(val ?? 0), row?.currency), row?.fullName ?? ''];
                        }}
                        labelFormatter={() => ''}
                      />
                      <Bar dataKey="value" radius={[0, 6, 6, 0]} maxBarSize={28}>
                        {topContracts.map((_, i) => (
                          <Cell key={i} fill={PIE_COLORS[i % PIE_COLORS.length]} />
                        ))}
                      </Bar>
                    </BarChart>
                  </ResponsiveContainer>
                </div>
              ) : (
                <p className="rounded-lg border border-dashed border-[#E4E7EC] px-4 py-10 text-center text-[13px] text-[#5B6478] dark:border-slate-700 dark:text-slate-400">
                  No contracts with a recorded value yet.
                </p>
              )}
            </Card>

            <Card>
              <SectionHeader title="Agreement types" subtitle="What kinds of contract the repository holds." icon={FileText} />
              {data.agreement_type_distribution.length ? (
                <div className="flex items-center gap-6">
                  <div className="h-[180px] w-[180px] shrink-0">
                    <ResponsiveContainer width="100%" height="100%">
                      <PieChart>
                        <Pie data={data.agreement_type_distribution.slice(0, 8)} dataKey="value" nameKey="label" cx="50%" cy="50%" innerRadius={46} outerRadius={82} strokeWidth={0} paddingAngle={2}>
                          {data.agreement_type_distribution.slice(0, 8).map((bucket, i) => (
                            <Cell key={bucket.label} fill={PIE_COLORS[i % PIE_COLORS.length]} />
                          ))}
                        </Pie>
                        <Tooltip contentStyle={tooltipStyle} formatter={(val, name) => [String(val), humanise(String(name)).toUpperCase()]} />
                      </PieChart>
                    </ResponsiveContainer>
                  </div>
                  <div className="min-w-0 flex-1 space-y-2.5">
                    {data.agreement_type_distribution.slice(0, 5).map((bucket, i) => (
                      <div key={bucket.label}>
                        <div className="flex items-center justify-between text-[12px]">
                          <span className="flex items-center gap-2 truncate font-medium text-slate-700 dark:text-slate-200">
                            <span className="inline-block h-2 w-2 shrink-0 rounded-full" style={{ background: PIE_COLORS[i % PIE_COLORS.length] }} />
                            <span className="truncate">{humanise(bucket.label.toLocaleUpperCase())}</span>
                          </span>
                          <span className="ml-2 shrink-0 font-semibold" style={{ color: PIE_COLORS[i % PIE_COLORS.length] }}>{bucket.percentage}%</span>
                        </div>
                        <div className="mt-1 h-1 w-full overflow-hidden rounded-full bg-slate-100 dark:bg-slate-700">
                          <div className="h-full rounded-full" style={{ width: `${bucket.percentage}%`, background: PIE_COLORS[i % PIE_COLORS.length] }} />
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              ) : (
                <p className="rounded-lg border border-dashed border-[#E4E7EC] px-4 py-10 text-center text-[13px] text-[#5B6478] dark:border-slate-700 dark:text-slate-400">
                  No agreements classified yet.
                </p>
              )}
            </Card>
          </div>

          {/* Row 2: Risk distribution + Status distribution */}
          <div className="grid gap-4 xl:grid-cols-2">
            <Card>
              <SectionHeader title="Risk distribution" subtitle="Contracts scored by risk band." icon={ShieldAlert} />
              {data.risk_distribution.length ? (
                <div className="flex items-end gap-6">
                  {/* Half-donut: cy at 100% so only the top semicircle is visible */}
                  <div className="h-[110px] w-[220px] shrink-0">
                    <ResponsiveContainer width="100%" height="100%">
                      <PieChart>
                        <Pie
                          data={data.risk_distribution}
                          dataKey="value"
                          nameKey="label"
                          cx="50%"
                          cy="100%"
                          startAngle={180}
                          endAngle={0}
                          innerRadius={55}
                          outerRadius={100}
                          strokeWidth={0}
                          paddingAngle={2}
                        >
                          {data.risk_distribution.map((bucket) => (
                            <Cell key={bucket.label} fill={RISK_FILLS[bucket.label.toLowerCase()] ?? '#94a3b8'} />
                          ))}
                        </Pie>
                        <Tooltip contentStyle={tooltipStyle} formatter={(val, name) => [String(val), humanise(String(name))]} />
                      </PieChart>
                    </ResponsiveContainer>
                  </div>
                  <div className="min-w-0 flex-1 space-y-2.5 pb-1">
                    {data.risk_distribution.map((bucket) => {
                      const color = RISK_FILLS[bucket.label.toLowerCase()] ?? '#94a3b8';
                      return (
                        <div key={bucket.label}>
                          <div className="flex items-center justify-between text-[12px]">
                            <span className="flex items-center gap-2 truncate font-medium text-slate-700 dark:text-slate-200">
                              <span className="inline-block h-2 w-2 shrink-0 rounded-full" style={{ background: color }} />
                              <span className="truncate">{humanise(bucket.label)}</span>
                            </span>
                            <span className="ml-2 shrink-0 font-bold tabular-nums" style={{ color }}>{bucket.percentage}%</span>
                          </div>
                          <div className="mt-1 h-1 w-full overflow-hidden rounded-full bg-slate-100 dark:bg-slate-700">
                            <div className="h-full rounded-full" style={{ width: `${bucket.percentage}%`, background: color }} />
                          </div>
                        </div>
                      );
                    })}
                  </div>
                </div>
              ) : (
                <p className="rounded-lg border border-dashed border-[#E4E7EC] px-4 py-10 text-center text-[13px] text-[#5B6478] dark:border-slate-700 dark:text-slate-400">
                  No risk scores yet. Scores appear once extraction finishes.
                </p>
              )}
            </Card>

            <Card>
              <SectionHeader title="Contract status" subtitle="Pipeline snapshot." icon={LayoutDashboard} />
              {data.status_distribution.length ? (
                <div className="flex items-center gap-6">
                  <div className="h-[180px] w-[180px] shrink-0">
                    <ResponsiveContainer width="100%" height="100%">
                      <PieChart>
                        <Pie data={data.status_distribution} dataKey="value" nameKey="label" cx="50%" cy="50%" innerRadius={46} outerRadius={82} strokeWidth={0} paddingAngle={2}>
                          {data.status_distribution.map((bucket) => (
                            <Cell key={bucket.label} fill={STATUS_FILLS[bucket.label.toLowerCase()] ?? '#94a3b8'} />
                          ))}
                        </Pie>
                        <Tooltip contentStyle={tooltipStyle} formatter={(val, name) => [String(val), humanise(String(name))]} />
                      </PieChart>
                    </ResponsiveContainer>
                  </div>
                  <div className="min-w-0 flex-1 space-y-2.5">
                    {data.status_distribution.map((bucket) => {
                      const color = STATUS_FILLS[bucket.label.toLowerCase()] ?? '#94a3b8';
                      return (
                        <div key={bucket.label}>
                          <div className="flex items-center justify-between text-[12px]">
                            <span className="flex items-center gap-2 truncate font-medium text-slate-700 dark:text-slate-200">
                              <span className="inline-block h-2 w-2 shrink-0 rounded-full" style={{ background: color }} />
                              <span className="truncate">{humanise(bucket.label)}</span>
                            </span>
                            <span className="ml-2 shrink-0 font-bold tabular-nums" style={{ color }}>{bucket.value}</span>
                          </div>
                          <div className="mt-1 h-1 w-full overflow-hidden rounded-full bg-slate-100 dark:bg-slate-700">
                            <div className="h-full rounded-full" style={{ width: `${bucket.percentage}%`, background: color }} />
                          </div>
                        </div>
                      );
                    })}
                  </div>
                </div>
              ) : (
                <p className="rounded-lg border border-dashed border-[#E4E7EC] px-4 py-10 text-center text-[13px] text-[#5B6478] dark:border-slate-700 dark:text-slate-400">
                  No status data yet.
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

function MetricTile({
  label,
  value,
  icon: Icon,
  kpiKey,
  onClick,
}: {
  label: string;
  value: React.ReactNode;
  icon: typeof FileText;
  kpiKey: string;
  onClick?: () => void;
}) {
  const { accent, bar } = getKpiColor(kpiKey);

  const inner = (
    <>
      {/* Colored top accent bar */}
      <div className={['h-1 w-full', bar].join(' ')} />
      <div className="p-5">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0 flex-1">
            <p className="text-[11px] font-semibold uppercase tracking-wider text-slate-400 dark:text-slate-300">
              {label}
            </p>
            <p className="mt-3 font-display text-[28px] font-bold leading-none text-[#0F172A] dark:text-slate-100">
              {value}
            </p>
          </div>
          <div className={['shrink-0 rounded-xl p-2.5', accent].join(' ')}>
            <Icon className="h-5 w-5" />
          </div>
        </div>
        {onClick && (
          <p className="mt-4 flex items-center gap-1 text-[11px] font-semibold text-slate-400 transition group-hover:text-blue-500 dark:text-slate-400 dark:group-hover:text-blue-400">
            View details <ArrowRight className="h-3 w-3" />
          </p>
        )}
      </div>
    </>
  );

  if (onClick) {
    return (
      <button
        type="button"
        onClick={onClick}
        className="group overflow-hidden rounded-xl border-x border-b border-[#E4E7EC] bg-white text-left shadow-sm transition hover:border-blue-300 hover:shadow-md dark:border-slate-700 dark:bg-slate-800 dark:hover:border-blue-700"
      >
        {inner}
      </button>
    );
  }
  return (
    <div className="overflow-hidden rounded-xl border-x border-b border-[#E4E7EC] bg-white shadow-sm dark:border-slate-700 dark:bg-slate-800">
      {inner}
    </div>
  );
}

export default DashboardPage;
