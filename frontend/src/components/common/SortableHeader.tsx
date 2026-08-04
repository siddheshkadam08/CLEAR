/**
 * A sortable column heading.
 *
 * Extracted from seven near-identical `<th>` blocks on the contracts table, each
 * repeating the same chevron ternary inline. Beyond the duplication, none of them
 * told assistive technology anything: a screen reader announced "Contract,
 * button" with no indication the column was sorted, which way, or that pressing
 * it would re-sort. `aria-sort` on the header cell is what conveys that, and it
 * belongs on the `<th>` rather than the button.
 */

import { ChevronDown, ChevronUp } from 'lucide-react';

export type SortDirection = 'asc' | 'desc';

export const SortableHeader = ({
  label,
  field,
  activeField,
  direction,
  onSort,
  align = 'left',
}: {
  label: string;
  field: string;
  activeField?: string;
  direction?: string;
  onSort: (field: string) => void;
  align?: 'left' | 'right';
}) => {
  const active = activeField === field;
  const ascending = direction === 'asc';

  return (
    <th
      scope="col"
      // `none` rather than omitting it: a sortable column that is not currently
      // sorted still needs to announce that sorting is available.
      aria-sort={active ? (ascending ? 'ascending' : 'descending') : 'none'}
      className={[
        'px-5 py-3.5 font-semibold uppercase tracking-[0.06em] text-slate-500 dark:text-slate-400',
        align === 'right' ? 'text-right' : '',
      ].join(' ')}
    >
      <button
        type="button"
        onClick={() => onSort(field)}
        title={`Sort by ${label.toLowerCase()}`}
        className={[
          'inline-flex items-center gap-1 rounded transition',
          'hover:text-[#0F172A] dark:hover:text-slate-100',
          'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-600',
          active ? 'text-[#0F172A] dark:text-slate-100' : '',
        ].join(' ')}
      >
        {label}
        {active ? (
          ascending ? (
            <ChevronUp className="h-3.5 w-3.5" />
          ) : (
            <ChevronDown className="h-3.5 w-3.5" />
          )
        ) : (
          // Dimmed rather than absent, so the column does not shift by an icon
          // width the moment it becomes the sorted one.
          <ChevronDown className="h-3.5 w-3.5 opacity-30" aria-hidden />
        )}
      </button>
    </th>
  );
};
