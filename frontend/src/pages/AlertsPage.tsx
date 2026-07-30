/**
 * Alerts.
 *
 * Deadline alerts are the one place this product acts on time rather than on a
 * question, so the default view is open alerts sorted by urgency. Acknowledging is
 * distinct from resolving: an auto-renewal notice window that has been *seen* is not
 * the same as one that has been *handled*, and collapsing the two loses the
 * distinction that matters when the window closes.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { BellRing } from 'lucide-react';
import { useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';

import { alerts as alertsApi } from '@/api/endpoints';
import { errorMessage } from '@/api/errors';
import type { Alert, AlertStatus } from '@/api/types';
import { Badge } from '@/components/common/Badge';
import { formatStatusLabel, getRiskVariant, getStatusVariant } from '@/lib/badges';
import { ErrorBanner } from '@/components/common/Banner';
import { Button } from '@/components/common/Button';
import { Card, PageHeader } from '@/components/common/Card';
import { EmptyState } from '@/components/common/EmptyState';
import { inputClasses } from '@/components/common/Field';
import { LoadingSpinner } from '@/components/common/LoadingSpinner';
import { daysUntil, formatDate, formatDateTime, humanise } from '@/lib/format';
import { useProjectScope } from '@/lib/scope';

const STATUSES: AlertStatus[] = ['open', 'acknowledged', 'escalated', 'resolved', 'dismissed'];
const SEVERITY_ORDER: Record<string, number> = { critical: 0, high: 1, medium: 2, low: 3 };
const DEFAULT_STATUSES = ['open', 'escalated'];

export function AlertsPage() {
  const { projectId } = useProjectScope();
  const [params, setParams] = useSearchParams();

  // Defaults to open alerts: a list dominated by resolved items is a list nobody
  // reads, and the whole point is that these are actionable.
  const selected = params.getAll('status');
  const statuses = selected.length ? selected : DEFAULT_STATUSES;

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ['alerts', projectId, statuses.join(',')],
    queryFn: () =>
      alertsApi.list({ project_id: projectId ?? undefined, status: statuses, page: 1 }),
  });

  const sorted = [...(data?.items ?? [])].sort((a, b) => {
    const bySeverity = (SEVERITY_ORDER[a.severity] ?? 9) - (SEVERITY_ORDER[b.severity] ?? 9);
    if (bySeverity !== 0) return bySeverity;
    const left = daysUntil(a.due_date) ?? 9999;
    const right = daysUntil(b.due_date) ?? 9999;
    return left - right;
  });

  return (
    <div className="space-y-5">
      <PageHeader
        title="Alerts"
        subtitle="Expiries, renewal notice windows and obligation deadlines across your projects."
      />

      <Card dense>
        <div className="flex flex-wrap gap-2">
          {STATUSES.map((status) => {
            const active = statuses.includes(status);
            return (
              <button
                key={status}
                type="button"
                aria-pressed={active}
                onClick={() => {
                  const next = new URLSearchParams(params);
                  const current = next.getAll('status').length
                    ? next.getAll('status')
                    : DEFAULT_STATUSES;
                  next.delete('status');
                  const updated = current.includes(status)
                    ? current.filter((entry) => entry !== status)
                    : [...current, status];
                  // Deselecting everything would show nothing at all, which reads as
                  // "no alerts". Fall back to the default view instead.
                  for (const entry of updated.length ? updated : DEFAULT_STATUSES) {
                    next.append('status', entry);
                  }
                  setParams(next, { replace: true });
                }}
                className={[
                  'rounded-full px-3 py-1.5 text-xs font-medium ring-1 ring-inset transition',
                  active
                    ? 'bg-blue-600 text-white ring-blue-600'
                    : 'bg-white text-slate-600 ring-slate-200 hover:bg-slate-50',
                ].join(' ')}
              >
                {humanise(status)}
              </button>
            );
          })}
        </div>
      </Card>

      {error ? (
        <ErrorBanner message={errorMessage(error)} onRetry={() => void refetch()} />
      ) : null}

      {isLoading ? (
        <Card>
          <LoadingSpinner label="Loading alerts..." />
        </Card>
      ) : sorted.length ? (
        <div className="space-y-3">
          {sorted.map((alert) => (
            <AlertRow key={alert.id} alert={alert} />
          ))}
        </div>
      ) : (
        <EmptyState
          icon={BellRing}
          title="Nothing needs attention"
          description="Alerts are raised as contracts approach expiry, an auto-renewal notice deadline, or a dated obligation. Nothing in the current filter is outstanding."
        />
      )}
    </div>
  );
}

function AlertRow({ alert }: { alert: Alert }) {
  const queryClient = useQueryClient();
  const [note, setNote] = useState('');
  const [noting, setNoting] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  const update = useMutation({
    mutationFn: (status: AlertStatus) =>
      alertsApi.update(alert.id, status, note.trim() || undefined),
    onSuccess: async () => {
      setNote('');
      setNoting(false);
      setActionError(null);
      await queryClient.invalidateQueries({ queryKey: ['alerts'] });
    },
    onError: (caught) => setActionError(errorMessage(caught)),
  });

  const remaining = daysUntil(alert.due_date);
  const overdue = remaining !== null && remaining < 0;
  const imminent = remaining !== null && remaining >= 0 && remaining <= 14;

  return (
    <Card
      dense
      className={
        overdue
          ? 'border-l-4 border-l-rose-500'
          : imminent
            ? 'border-l-4 border-l-amber-400'
            : ''
      }
    >
      <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <Badge text={humanise(alert.severity)} variant={getRiskVariant(alert.severity)} />
            <span className="font-semibold text-slate-900">{alert.title}</span>
            <Badge text={humanise(alert.alert_type)} variant="neutral" />
            <Badge
              text={formatStatusLabel(alert.status)}
              variant={getStatusVariant(alert.status)}
            />
          </div>

          <p className="mt-2 text-sm leading-6 text-slate-600">{alert.message}</p>

          <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-slate-500">
            {alert.contract_id ? (
              <Link
                to={`/contracts/${alert.contract_id}`}
                className="font-medium text-blue-600 hover:text-blue-700"
              >
                {alert.contract_title ?? 'Open contract'}
              </Link>
            ) : null}
            {alert.due_date ? (
              <span
                className={overdue ? 'text-rose-600' : imminent ? 'text-amber-600' : undefined}
              >
                Due {formatDate(alert.due_date)}
                {remaining !== null
                  ? overdue
                    ? ` · ${Math.abs(remaining)} days overdue`
                    : ` · in ${remaining} days`
                  : ''}
              </span>
            ) : null}
            <span>Raised {formatDateTime(alert.created_at)}</span>
          </div>

          {alert.note ? (
            <p className="mt-2 rounded-xl bg-slate-50 px-3 py-2 text-xs text-slate-600">
              Note: {alert.note}
            </p>
          ) : null}

          {noting ? (
            <textarea
              rows={2}
              placeholder="What was done, or why this is being dismissed."
              value={note}
              onChange={(event) => setNote(event.target.value)}
              className={`${inputClasses} mt-3 max-w-lg resize-y`}
            />
          ) : null}

          {actionError ? (
            <div className="mt-3">
              <ErrorBanner message={actionError} />
            </div>
          ) : null}
        </div>

        <div className="flex flex-wrap gap-2 lg:shrink-0 lg:flex-col lg:items-stretch">
          {alert.status === 'open' ? (
            <Button
              variant="secondary"
              size="sm"
              busy={update.isPending}
              onClick={() => update.mutate('acknowledged')}
            >
              Acknowledge
            </Button>
          ) : null}
          {alert.status !== 'resolved' ? (
            <Button size="sm" busy={update.isPending} onClick={() => update.mutate('resolved')}>
              Resolve
            </Button>
          ) : null}
          {alert.status !== 'dismissed' && alert.status !== 'resolved' ? (
            <Button
              variant="secondary"
              size="sm"
              busy={update.isPending}
              onClick={() => update.mutate('dismissed')}
            >
              Dismiss
            </Button>
          ) : null}
          {!noting ? (
            <Button variant="ghost" size="sm" onClick={() => setNoting(true)}>
              Add note
            </Button>
          ) : null}
        </div>
      </div>
    </Card>
  );
}

export default AlertsPage;
