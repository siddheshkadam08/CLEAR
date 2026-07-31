/**
 * Labelled form field.
 *
 * The label is a real `<label>` wrapping the control, so tapping it focuses the
 * input - which matters far more on a phone than on a desktop.
 */

import type { ReactNode } from 'react';

export const inputClasses =
  'w-full rounded-xl border border-slate-200 px-3 py-2.5 text-sm text-slate-900 outline-none transition placeholder:text-slate-400 focus:border-blue-500 focus:ring-4 focus:ring-blue-100 disabled:bg-slate-50 disabled:text-slate-500 dark:border-slate-600 dark:bg-slate-800 dark:text-slate-100 dark:placeholder:text-slate-500 dark:focus:ring-blue-900 dark:disabled:bg-slate-900';

export const selectClasses =
  'h-9 w-full cursor-pointer appearance-none rounded-lg border border-[#E4E7EC] bg-white py-0 pl-3 pr-8 text-[13px] font-medium text-[#0F172A] outline-none transition hover:border-[#94A0B4] focus:border-[#2563EB] focus:ring-2 focus:ring-blue-100 disabled:cursor-not-allowed disabled:opacity-50 dark:border-slate-600 dark:bg-slate-800 dark:text-slate-100 dark:hover:border-slate-500 dark:focus:ring-blue-900';

export const SelectChevron = () => (
  <svg className="pointer-events-none absolute right-2.5 top-1/2 -translate-y-1/2 text-[#5B6478]" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><path d="M6 9l6 6 6-6" /></svg>
);

export const Field = ({
  label,
  hint,
  required = false,
  children,
}: {
  label: string;
  hint?: string;
  required?: boolean;
  children: ReactNode;
}) => (
  <label className="block space-y-1.5">
    <span className="text-sm font-medium text-slate-700">
      {label}
      {required ? <span className="ml-0.5 text-rose-500">*</span> : null}
    </span>
    {children}
    {hint ? <span className="block text-xs text-slate-500">{hint}</span> : null}
  </label>
);

export default Field;
