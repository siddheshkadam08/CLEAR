/**
 * Alerts, and the rules that produce them.
 *
 * Deadline alerts are the one place this product acts on time rather than on a
 * question, so the default view is open alerts sorted by urgency. Acknowledging is
 * distinct from resolving: an auto-renewal notice window that has been *seen* is not
 * the same as one that has been *handled*, and collapsing the two loses the
 * distinction that matters when the window closes.
 *
 * The second tab is the configuration behind the first. It is not admin-only,
 * deliberately: "why am I being told this?" is a fair question from whoever
 * received the alert, and the answer is a threshold on this screen. Only a system
 * administrator can *change* one - the server enforces that, and the controls are
 * rendered read-only for everyone else rather than hidden, so the thresholds stay
 * legible.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { BellRing, Plus, SlidersHorizontal, Trash2 } from 'lucide-react';
import { useEffect, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';

import { alerts as alertsApi } from '@/api/endpoints';
import { errorMessage } from '@/api/errors';
import type { Alert, AlertRule, AlertSeverity, AlertStatus, AlertType } from '@/api/types';
import { Badge } from '@/components/common/Badge';
import { formatStatusLabel, getRiskVariant, getStatusVariant } from '@/lib/badges';
import { ErrorBanner } from '@/components/common/Banner';
import { Button } from '@/components/common/Button';
import { Card, PageHeader } from '@/components/common/Card';
import { EmptyState } from '@/components/common/EmptyState';
import { FilterChip } from '@/components/common/FilterChip';
import { inputClasses, selectClasses, SelectChevron } from '@/components/common/Field';
import { LoadingSpinner } from '@/components/common/LoadingSpinner';
import { useCanAdminister } from '@/lib/auth';
import { daysUntil, formatDate, formatDateTime, humanise } from '@/lib/format';
import { useProjectScope } from '@/lib/scope';

const STATUSES: AlertStatus[] = ['open', 'acknowledged', 'resolved', 'dismissed'];
const SEVERITY_ORDER: Record<string, number> = { critical: 0, high: 1, medium: 2, low: 3 };
const DEFAULT_STATUSES: AlertStatus[] = ['open'];
const SEVERITIES: AlertSeverity[] = ['critical', 'high', 'medium', 'low', 'info'];

export function AlertsPage() {
  const [params, setParams] = useSearchParams();
  const tab = params.get('tab') === 'rules' ? 'rules' : 'alerts';

  return (
    <div className="space-y-5">
      <PageHeader
        title="Alerts"
        subtitle="Expiries, renewal notice windows and obligation deadlines across your projects."
      />

      <div className="flex gap-1 border-b border-slate-200 dark:border-slate-700">
        {(['alerts', 'rules'] as const).map((name) => (
          <button
            key={name}
            type="button"
            aria-current={tab === name ? 'page' : undefined}
            onClick={() => {
              const next = new URLSearchParams(params);
              if (name === 'rules') next.set('tab', 'rules');
              else next.delete('tab');
              setParams(next, { replace: true });
            }}
            className={[
              '-mb-px border-b-2 px-4 py-2.5 text-sm font-medium transition',
              tab === name
                ? 'border-blue-600 text-blue-700 dark:text-blue-400'
                : 'border-transparent text-slate-500 hover:text-slate-800 dark:hover:text-slate-200',
            ].join(' ')}
          >
            {name === 'alerts' ? 'Open alerts' : 'Alert rules'}
          </button>
        ))}
      </div>

      {tab === 'rules' ? <RulesTab /> : <AlertsTab />}
    </div>
  );
}

// =============================================================================
// Alerts
// =============================================================================
function AlertsTab() {
  const { projectId } = useProjectScope();
  const [params, setParams] = useSearchParams();

  // Defaults to open alerts: a list dominated by resolved items is a list nobody
  // reads, and the whole point is that these are actionable.
  const selected = params.getAll('status') as AlertStatus[];
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
    <>
      <Card dense>
        <div className="flex flex-wrap gap-2">
          {STATUSES.map((status) => {
            const active = statuses.includes(status);
            return (
              <FilterChip
                key={status}
                label={humanise(status)}
                active={active}
                onClick={() => {
                  const next = new URLSearchParams(params);
                  const current = (next.getAll('status').length
                    ? next.getAll('status')
                    : DEFAULT_STATUSES) as AlertStatus[];
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
              />
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
    </>
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
            <span className="font-semibold text-slate-900 dark:text-slate-100">{alert.title}</span>
            <Badge text={humanise(alert.alert_type)} variant="neutral" />
            <Badge
              text={formatStatusLabel(alert.status)}
              variant={getStatusVariant(alert.status)}
            />
          </div>

          <p className="mt-2 whitespace-pre-line text-sm leading-6 text-slate-600">
            {alert.message}
          </p>

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

// =============================================================================
// Rules
// =============================================================================
/**
 * The tunable knobs per alert type.
 *
 * These keys are read by `app/services/alert_evaluator.py`, and only these. A
 * key added here that the evaluator does not read is a control that does
 * nothing, so the two lists have to move together.
 */
const RULE_FIELDS: Record<AlertType, { key: string; label: string; hint: string }[]> = {
  contract_expiring: [
    { key: 'window_days', label: 'Warn within (days)', hint: 'How far ahead of expiry to start warning.' },
    { key: 'escalate_days', label: 'High within (days)', hint: 'Inside this many days the alert becomes High.' },
    { key: 'critical_days', label: 'Critical within (days)', hint: 'Inside this many days it becomes Critical.' },
  ],
  auto_renewal_notice: [
    { key: 'lead_days', label: 'Warn within (days)', hint: 'How far ahead of the notice deadline to warn.' },
    { key: 'critical_days', label: 'Critical within (days)', hint: 'Inside this many days it becomes Critical.' },
  ],
  obligation_due: [
    { key: 'window_days', label: 'Due within (days)', hint: 'How far ahead of the due date to warn.' },
    { key: 'overdue_days', label: 'Keep overdue for (days)', hint: 'How long a missed obligation keeps alerting.' },
  ],
  high_risk: [
    { key: 'risk_score_cutoff', label: 'Risk score at or above', hint: 'Scores run 0-100.' },
    { key: 'critical_score', label: 'Critical at or above', hint: 'Above this the alert becomes Critical.' },
  ],
  missing_mandatory_clause: [
    { key: 'min_missing', label: 'Minimum missing clauses', hint: 'Fewer than this does not raise an alert.' },
  ],
  review_required: [
    { key: 'min_items', label: 'Minimum items awaiting review', hint: 'Clauses queued for a human reviewer.' },
  ],
  // Raised by the pipeline, not by the evaluator. Shown so it can be switched
  // off, but it has no thresholds this screen can offer.
  processing_failed: [],
};

const RULE_TYPES = Object.keys(RULE_FIELDS) as AlertType[];

function RulesTab() {
  const { projectId, projects } = useProjectScope();
  const isAdmin = useCanAdminister();
  const queryClient = useQueryClient();
  const [creating, setCreating] = useState(false);

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ['alert-rules', projectId],
    queryFn: () => alertsApi.rules.list(projectId),
  });

  const rules = data ?? [];
  const projectName = projects.find((entry) => entry.id === projectId)?.name;
  const overridden = new Set(rules.filter((rule) => rule.project_id).map((rule) => rule.alert_type));
  const canOverride = Boolean(isAdmin && projectId && overridden.size < RULE_TYPES.length);

  return (
    <>
      <Card dense>
        <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <p className="text-sm text-slate-600 dark:text-slate-300">
            {projectId
              ? `Platform defaults, plus any override set for ${projectName ?? 'this project'}.`
              : 'Platform defaults. Select a project above to add an override for it.'}{' '}
            The scheduler re-evaluates every contract against these on its own cadence.
          </p>
          {canOverride ? (
            <Button size="sm" onClick={() => setCreating(true)}>
              <Plus className="mr-1.5 h-3.5 w-3.5" />
              Project override
            </Button>
          ) : null}
        </div>
      </Card>

      {!isAdmin ? (
        <p className="text-xs text-slate-500">
          These thresholds are read-only for you. A system administrator can change them.
        </p>
      ) : null}

      {error ? (
        <ErrorBanner message={errorMessage(error)} onRetry={() => void refetch()} />
      ) : null}

      {creating && projectId ? (
        <CreateOverride
          projectId={projectId}
          taken={overridden}
          onClose={() => setCreating(false)}
          onCreated={async () => {
            setCreating(false);
            await queryClient.invalidateQueries({ queryKey: ['alert-rules'] });
          }}
        />
      ) : null}

      {isLoading ? (
        <Card>
          <LoadingSpinner label="Loading rules..." />
        </Card>
      ) : rules.length ? (
        <div className="space-y-3">
          {rules.map((rule) => (
            <RuleCard key={rule.id} rule={rule} editable={isAdmin} />
          ))}
        </div>
      ) : (
        <EmptyState
          icon={SlidersHorizontal}
          title="No alert rules are configured"
          description="Without a rule the evaluator has no threshold to apply, so no alerts of that type are ever raised. Re-run the seed to restore the platform defaults."
        />
      )}
    </>
  );
}

function RuleCard({ rule, editable }: { rule: AlertRule; editable: boolean }) {
  const queryClient = useQueryClient();
  const [severity, setSeverity] = useState<AlertSeverity>(rule.severity);
  const [escalate, setEscalate] = useState(String(rule.escalate_after_days ?? ''));
  const [config, setConfig] = useState<Record<string, string>>(() => toFormConfig(rule));
  const [actionError, setActionError] = useState<string | null>(null);

  // Re-seeded when the query refetches, so a save elsewhere does not leave this
  // card showing what the user typed over what is now stored.
  useEffect(() => {
    setSeverity(rule.severity);
    setEscalate(String(rule.escalate_after_days ?? ''));
    setConfig(toFormConfig(rule));
  }, [rule]);

  const invalidate = () => queryClient.invalidateQueries({ queryKey: ['alert-rules'] });

  const save = useMutation({
    mutationFn: () =>
      alertsApi.rules.update(rule.id, {
        severity,
        escalate_after_days: escalate.trim() ? Number(escalate) : null,
        config: {
          ...rule.config,
          ...Object.fromEntries(
            Object.entries(config)
              .filter(([, value]) => value.trim() !== '')
              .map(([key, value]) => [key, Number(value)]),
          ),
        },
      }),
    onSuccess: async () => {
      setActionError(null);
      await invalidate();
    },
    onError: (caught) => setActionError(errorMessage(caught)),
  });

  const toggle = useMutation({
    mutationFn: () => alertsApi.rules.update(rule.id, { is_enabled: !rule.is_enabled }),
    onSuccess: async () => {
      setActionError(null);
      await invalidate();
    },
    onError: (caught) => setActionError(errorMessage(caught)),
  });

  const remove = useMutation({
    mutationFn: () => alertsApi.rules.remove(rule.id),
    onSuccess: async () => {
      setActionError(null);
      await invalidate();
    },
    onError: (caught) => setActionError(errorMessage(caught)),
  });

  const fields = RULE_FIELDS[rule.alert_type] ?? [];
  const dirty =
    severity !== rule.severity ||
    escalate !== String(rule.escalate_after_days ?? '') ||
    fields.some(({ key }) => (config[key] ?? '') !== stringify(rule.config[key]));

  return (
    <Card dense className={rule.is_enabled ? '' : 'opacity-60'}>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-semibold text-slate-900 dark:text-slate-100">{rule.name}</span>
          <Badge text={humanise(rule.alert_type)} variant="neutral" />
          <Badge
            text={rule.project_id ? 'Project override' : 'Platform default'}
            variant={rule.project_id ? 'info' : 'neutral'}
          />
          {!rule.is_enabled ? <Badge text="Disabled" variant="warning" /> : null}
        </div>

        <div className="flex items-center gap-2">
          <Button
            variant="secondary"
            size="sm"
            disabled={!editable}
            busy={toggle.isPending}
            onClick={() => toggle.mutate()}
          >
            {rule.is_enabled ? 'Disable' : 'Enable'}
          </Button>
          {/* Only overrides can be deleted. Removing a platform default would
              leave that alert type with no rule at all, which silently stops
              the evaluator from ever raising it. */}
          {editable && rule.project_id ? (
            <Button
              variant="ghost"
              size="sm"
              busy={remove.isPending}
              onClick={() => remove.mutate()}
              aria-label={`Delete the ${rule.name} override`}
            >
              <Trash2 className="h-3.5 w-3.5" />
            </Button>
          ) : null}
        </div>
      </div>

      {rule.alert_type === 'processing_failed' ? (
        <p className="mt-3 text-xs text-slate-500">
          Raised by the processing pipeline when a document fails every retry, not by the
          scheduled evaluator. There is nothing to tune beyond switching it off.
        </p>
      ) : null}

      <div className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <label className="block space-y-1">
          <span className="text-xs font-medium text-slate-600 dark:text-slate-300">Severity</span>
          <div className="relative">
            <select
              value={severity}
              disabled={!editable}
              onChange={(event) => setSeverity(event.target.value as AlertSeverity)}
              className={selectClasses}
            >
              {SEVERITIES.map((value) => (
                <option key={value} value={value}>
                  {humanise(value)}
                </option>
              ))}
            </select>
            <SelectChevron />
          </div>
        </label>

        {fields.map(({ key, label, hint }) => (
          <label key={key} className="block space-y-1">
            <span className="text-xs font-medium text-slate-600 dark:text-slate-300">{label}</span>
            <input
              type="number"
              min={0}
              inputMode="numeric"
              value={config[key] ?? ''}
              disabled={!editable}
              onChange={(event) =>
                setConfig((current) => ({ ...current, [key]: event.target.value }))
              }
              className={`${inputClasses} h-9 py-0`}
            />
            <span className="block text-[11px] leading-4 text-slate-400">{hint}</span>
          </label>
        ))}

        <label className="block space-y-1">
          <span className="text-xs font-medium text-slate-600 dark:text-slate-300">
            Escalate after (days)
          </span>
          <input
            type="number"
            min={1}
            inputMode="numeric"
            value={escalate}
            disabled={!editable}
            onChange={(event) => setEscalate(event.target.value)}
            className={`${inputClasses} h-9 py-0`}
          />
          <span className="block text-[11px] leading-4 text-slate-400">
            An alert nobody has actioned in this long is raised one severity. Blank means never.
          </span>
        </label>
      </div>

      {actionError ? (
        <div className="mt-3">
          <ErrorBanner message={actionError} />
        </div>
      ) : null}

      {editable && dirty ? (
        <div className="mt-4 flex items-center gap-2">
          <Button size="sm" busy={save.isPending} onClick={() => save.mutate()}>
            Save changes
          </Button>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => {
              setSeverity(rule.severity);
              setEscalate(String(rule.escalate_after_days ?? ''));
              setConfig(toFormConfig(rule));
            }}
          >
            Discard
          </Button>
        </div>
      ) : null}
    </Card>
  );
}

function CreateOverride({
  projectId,
  taken,
  onClose,
  onCreated,
}: {
  projectId: string;
  taken: Set<AlertType>;
  onClose: () => void;
  onCreated: () => void | Promise<void>;
}) {
  const available = RULE_TYPES.filter((type) => !taken.has(type));
  const [alertType, setAlertType] = useState<AlertType>(available[0] ?? 'contract_expiring');
  const [name, setName] = useState('');
  const [actionError, setActionError] = useState<string | null>(null);

  const create = useMutation({
    mutationFn: () =>
      alertsApi.rules.create({
        name: name.trim() || humanise(alertType),
        alert_type: alertType,
        severity: 'medium',
        is_enabled: true,
        // Empty: every threshold the evaluator reads has a documented default,
        // so an override starts by behaving exactly like the platform rule and
        // diverges only where the administrator actually types a number.
        config: {},
        notify_channels: ['in_app'],
        project_id: projectId,
      }),
    onSuccess: () => {
      setActionError(null);
      void onCreated();
    },
    onError: (caught) => setActionError(errorMessage(caught)),
  });

  return (
    <Card dense>
      <p className="text-sm font-semibold text-slate-900 dark:text-slate-100">
        Override a rule for this project
      </p>
      <p className="mt-1 text-xs text-slate-500">
        The override starts with the same behaviour as the platform default. Change a threshold
        on it afterwards to make this project differ.
      </p>

      <div className="mt-3 flex flex-col gap-3 sm:flex-row sm:items-end">
        <label className="block flex-1 space-y-1">
          <span className="text-xs font-medium text-slate-600 dark:text-slate-300">Alert type</span>
          <div className="relative">
            <select
              value={alertType}
              onChange={(event) => setAlertType(event.target.value as AlertType)}
              className={selectClasses}
            >
              {available.map((type) => (
                <option key={type} value={type}>
                  {humanise(type)}
                </option>
              ))}
            </select>
            <SelectChevron />
          </div>
        </label>

        <label className="block flex-1 space-y-1">
          <span className="text-xs font-medium text-slate-600 dark:text-slate-300">Name</span>
          <input
            value={name}
            placeholder={humanise(alertType)}
            onChange={(event) => setName(event.target.value)}
            className={`${inputClasses} h-9 py-0`}
          />
        </label>

        <div className="flex gap-2">
          <Button
            size="sm"
            busy={create.isPending}
            disabled={available.length === 0}
            onClick={() => create.mutate()}
          >
            Create
          </Button>
          <Button variant="ghost" size="sm" onClick={onClose}>
            Cancel
          </Button>
        </div>
      </div>

      {actionError ? (
        <div className="mt-3">
          <ErrorBanner message={actionError} />
        </div>
      ) : null}
    </Card>
  );
}

/** Config values as form strings, blank where the rule does not set the key. */
function toFormConfig(rule: AlertRule): Record<string, string> {
  const fields = RULE_FIELDS[rule.alert_type] ?? [];
  return Object.fromEntries(fields.map(({ key }) => [key, stringify(rule.config[key])]));
}

function stringify(value: unknown): string {
  return value === undefined || value === null ? '' : String(value);
}

export default AlertsPage;
