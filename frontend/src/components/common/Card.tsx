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
      'rounded-2xl border border-slate-200 bg-white shadow-sm',
      dense ? 'p-5' : 'p-6',
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
  <div className="mb-6 flex items-start justify-between gap-4">
    <div className="flex items-center gap-3">
      {Icon ? (
        <div className="rounded-xl bg-blue-50 p-2 text-blue-600">
          <Icon className="h-5 w-5" />
        </div>
      ) : null}
      <div>
        <h3 className="text-lg font-semibold text-slate-900">{title}</h3>
        <p className="text-sm text-slate-500">{subtitle}</p>
      </div>
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
  <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
    <div>
      <h1 className="text-xl font-semibold text-slate-900">{title}</h1>
      <p className="text-sm text-slate-500">{subtitle}</p>
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
    <div className="flex items-start justify-between">
      <div>
        <p className="text-sm font-medium text-slate-500">{label}</p>
        <p className="mt-4 text-3xl font-semibold text-slate-900">{value}</p>
      </div>
      <div className={['rounded-2xl p-3', accent].join(' ')}>
        <Icon className="h-6 w-6" />
      </div>
    </div>
  );

  if (onClick) {
    return (
      <button
        type="button"
        onClick={onClick}
        className="rounded-2xl border border-slate-200 bg-white p-6 text-left shadow-sm transition hover:border-blue-200 hover:shadow-md"
      >
        {body}
      </button>
    );
  }
  return (
    <div className="rounded-2xl border border-slate-200 bg-white p-6 shadow-sm">{body}</div>
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
  <div className="animate-pulse rounded-2xl border border-slate-200 bg-white p-6 shadow-sm">
    <div className="h-10 w-10 rounded-xl bg-slate-200" />
    <div className="mt-5 h-4 w-28 rounded bg-slate-200" />
    <div className="mt-3 h-8 w-20 rounded bg-slate-200" />
  </div>
);
