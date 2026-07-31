/**
 * Card shell and section header.
 *
 * Every section header carries a subtitle by contract - a bare heading tells the
 * reader what a thing is called but not why it is on screen.
 */

import type { LucideIcon } from 'lucide-react';
import type { ReactNode } from 'react';

export const Card = ({
  children,
  className = '',
  dense = false,
}: {
  children: ReactNode;
  className?: string;
  dense?: boolean;
}) => (
  <div
    className={[
      'rounded-xl border border-[#E4E7EC] bg-white shadow-sm dark:border-slate-700 dark:bg-slate-800',
      dense ? 'p-4' : 'p-[18px]',
      className,
    ].join(' ')}
  >
    {children}
  </div>
);

export const SectionHeader = ({
  title,
  subtitle,
  icon: Icon,
  action,
}: {
  title: string;
  subtitle: string;
  icon?: LucideIcon;
  action?: ReactNode;
}) => (
  <div className="mb-4 flex items-start justify-between gap-3">
    <div>
      <div className="flex items-center gap-2">
        {Icon ? <Icon className="h-4 w-4 shrink-0 text-[#94A0B4]" /> : null}
        <h3 className="text-sm font-semibold text-[#0F172A] dark:text-slate-100">{title}</h3>
      </div>
      {subtitle ? <p className="mt-0.5 text-xs text-[#5B6478] dark:text-slate-400">{subtitle}</p> : null}
    </div>
    {action ? <div className="flex shrink-0 items-center gap-2">{action}</div> : null}
  </div>
);

export const PageHeader = ({
  title,
  subtitle,
  actions,
}: {
  title: string;
  subtitle: string;
  actions?: ReactNode;
}) => (
  <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
    <div>
      <h1 className="text-[21px] font-semibold text-[#0F172A] dark:text-slate-100">{title}</h1>
      <p className="mt-0.5 text-[13px] text-[#5B6478] dark:text-slate-400">{subtitle}</p>
    </div>
    {actions ? <div className="flex flex-wrap items-center gap-2">{actions}</div> : null}
  </div>
);

/** KPI tile. Accent rotates through the tint sequence so a grid reads as a series. */
export const MetricCard = ({
  label,
  value,
  icon: Icon,
  accent,
  onClick,
}: {
  label: string;
  value: ReactNode;
  icon: LucideIcon;
  accent: string;
  onClick?: () => void;
}) => {
  const body = (
    <div className="flex items-start justify-between gap-3">
      <div className="min-w-0">
        <p className="text-[12.5px] text-[#5B6478] dark:text-slate-400">{label}</p>
        <p className="mt-2 text-[26px] font-semibold leading-tight text-[#0F172A] dark:text-slate-100">{value}</p>
      </div>
      <div className={['shrink-0 rounded-lg p-2', accent].join(' ')}>
        <Icon className="h-4 w-4" />
      </div>
    </div>
  );

  if (onClick) {
    return (
      <button
        type="button"
        onClick={onClick}
        className="rounded-xl border border-[#E4E7EC] bg-white p-[18px] text-left shadow-sm transition hover:border-blue-200 hover:shadow-md dark:border-slate-700 dark:bg-slate-800 dark:hover:border-blue-500"
      >
        {body}
      </button>
    );
  }
  return (
    <div className="rounded-xl border border-[#E4E7EC] bg-white p-[18px] shadow-sm dark:border-slate-700 dark:bg-slate-800">{body}</div>
  );
};

/** KPI accent tints, in the order a grid should cycle through them. */
export const ACCENTS = [
  'bg-blue-50 text-blue-600',
  'bg-indigo-50 text-indigo-600',
  'bg-emerald-50 text-emerald-600',
  'bg-orange-50 text-orange-600',
  'bg-violet-50 text-violet-600',
  'bg-sky-50 text-sky-600',
  'bg-teal-50 text-teal-600',
  'bg-amber-50 text-amber-600',
  'bg-rose-50 text-rose-600',
] as const;

export const KpiSkeleton = () => (
  <div className="animate-pulse rounded-xl border border-[#E4E7EC] bg-white p-[18px] shadow-sm dark:border-slate-700 dark:bg-slate-800">
    <div className="h-7 w-7 rounded-lg bg-slate-200" />
    <div className="mt-3 h-3 w-24 rounded bg-slate-200" />
    <div className="mt-2 h-7 w-16 rounded bg-slate-200" />
  </div>
);
