/**
 * Labelled form field.
 *
 * The label is a real `<label>` wrapping the control, so tapping it focuses the
 * input - which matters far more on a phone than on a desktop.
 */

import type { ReactNode } from 'react';

export const inputClasses =
  'w-full rounded-xl border border-slate-200 px-3 py-2.5 text-sm text-slate-900 outline-none transition placeholder:text-slate-400 focus:border-blue-500 focus:ring-4 focus:ring-blue-100 disabled:bg-slate-50 disabled:text-slate-500';

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
