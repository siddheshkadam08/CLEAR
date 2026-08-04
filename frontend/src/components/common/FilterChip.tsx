/**
 * A toggle-style filter pill.
 *
 * The same markup was written out five times - Alerts, Contracts, Exports, Jobs
 * and Portfolio - and had already drifted: the dark-mode classes were ordered
 * differently on each, one carried no focus ring, and ClauseMaster's near-copy
 * had no `aria-pressed` at all. A filter that a keyboard user cannot tell is on
 * is not a small omission; `aria-pressed` is the only thing that conveys state
 * here, since nothing about a coloured pill reaches a screen reader.
 *
 * A button with `aria-pressed`, not a checkbox: these toggle a view rather than
 * collect a value, and they do not submit.
 */

import type { ReactNode } from 'react';

export const FilterChip = ({
  label,
  active,
  onClick,
  count,
}: {
  label: ReactNode;
  active: boolean;
  onClick: () => void;
  /** Optional match count, shown after the label. */
  count?: number;
}) => (
  <button
    type="button"
    aria-pressed={active}
    onClick={onClick}
    className={[
      'rounded-full px-3 py-1.5 text-xs font-medium ring-1 ring-inset transition',
      'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-600',
      active
        ? 'bg-blue-600 text-white ring-blue-600'
        : 'bg-white text-slate-600 ring-slate-200 hover:bg-slate-50 dark:bg-slate-800 dark:text-slate-300 dark:ring-slate-600 dark:hover:bg-slate-700',
    ].join(' ')}
  >
    {label}
    {count !== undefined ? (
      <span className={['ml-1.5 tabular-nums', active ? 'text-blue-100' : 'text-slate-400'].join(' ')}>
        {count}
      </span>
    ) : null}
  </button>
);
