import { ChevronLeft, ChevronRight } from 'lucide-react';

interface PaginationProps {
  page: number;
  pages: number;
  total: number;
  pageSize: number;
  onPage: (p: number) => void;
}

/** Returns the page numbers to show, with null as an ellipsis placeholder. */
function pageRange(current: number, total: number): (number | null)[] {
  if (total <= 7) return Array.from({ length: total }, (_, i) => i + 1);
  if (current <= 4) return [1, 2, 3, 4, 5, null, total];
  if (current >= total - 3) return [1, null, total - 4, total - 3, total - 2, total - 1, total];
  return [1, null, current - 1, current, current + 1, null, total];
}

export const Pagination = ({ page, pages, total, pageSize, onPage }: PaginationProps) => {
  if (pages <= 1) return null;
  const nums = pageRange(page, pages);
  const from = (page - 1) * pageSize + 1;
  const to = Math.min(page * pageSize, total);

  return (
    <div className="flex flex-wrap items-center justify-between gap-3">
      <span className="text-[13px] text-slate-500 dark:text-slate-400">
        {from}–{to} of {total}
      </span>
      <div className="flex items-center gap-1">
        <button
          type="button"
          onClick={() => onPage(page - 1)}
          disabled={page === 1}
          className="flex h-8 w-8 items-center justify-center rounded-lg border border-slate-200 text-slate-500 transition hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-40 dark:border-slate-600 dark:text-slate-400 dark:hover:bg-slate-700"
        >
          <ChevronLeft className="h-4 w-4" />
        </button>
        {nums.map((n, i) =>
          n === null ? (
            <span key={`gap-${i}`} className="flex h-8 w-8 items-center justify-center text-[13px] text-slate-400 dark:text-slate-500">
              …
            </span>
          ) : (
            <button
              key={n}
              type="button"
              onClick={() => onPage(n)}
              className={[
                'flex h-8 min-w-[2rem] items-center justify-center rounded-lg px-2 text-[13px] font-medium transition',
                n === page
                  ? 'bg-blue-600 text-white shadow-sm'
                  : 'border border-slate-200 text-slate-600 hover:bg-slate-50 dark:border-slate-600 dark:text-slate-300 dark:hover:bg-slate-700',
              ].join(' ')}
            >
              {n}
            </button>
          )
        )}
        <button
          type="button"
          onClick={() => onPage(page + 1)}
          disabled={page === pages}
          className="flex h-8 w-8 items-center justify-center rounded-lg border border-slate-200 text-slate-500 transition hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-40 dark:border-slate-600 dark:text-slate-400 dark:hover:bg-slate-700"
        >
          <ChevronRight className="h-4 w-4" />
        </button>
      </div>
    </div>
  );
};

export default Pagination;
