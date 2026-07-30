/**
 * Inline error / success banner.
 *
 * Rendered above content rather than replacing it, so a stale-but-valid list stays
 * readable when a refresh fails.
 */

import { AlertCircle, CheckCircle2 } from 'lucide-react';

export const ErrorBanner = ({
  message,
  onRetry,
}: {
  message: string;
  onRetry?: () => void;
}) => (
  <div className="flex items-start gap-3 rounded-2xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700">
    <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
    <span className="flex-1">{message}</span>
    {onRetry ? (
      <button
        type="button"
        onClick={onRetry}
        className="rounded-lg border border-rose-200 px-2.5 py-1.5 text-xs font-medium text-rose-700 transition hover:bg-rose-100"
      >
        Try again
      </button>
    ) : null}
  </div>
);

export const SuccessBanner = ({ message }: { message: string }) => (
  <div className="flex items-start gap-3 rounded-2xl border border-emerald-200 bg-emerald-50 px-4 py-3 text-sm text-emerald-700">
    <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0" />
    <span>{message}</span>
  </div>
);

export const NoticeBanner = ({ message }: { message: string }) => (
  <div className="rounded-2xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800">
    {message}
  </div>
);
