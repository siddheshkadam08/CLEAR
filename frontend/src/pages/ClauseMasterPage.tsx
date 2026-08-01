/**
 * Clause Master.
 *
 * The configuration that drives extraction: which clause categories exist, which are
 * mandatory, what confidence they require, how they surface in the UI, and the
 * versioned rule that tells the model what to look for. Changing a category here
 * changes behaviour across the platform without a deployment.
 *
 * Rules are versioned rather than edited in place, so an extraction can always be
 * explained by the rule version that produced it. The current version is shown
 * alongside the rule for exactly that reason.
 *
 * Master/detail above `lg`; below it the list becomes a select, because a 260px
 * sidebar next to a detail pane on a phone leaves neither one usable.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Settings2 } from 'lucide-react';
import { useState } from 'react';

import { clauseMaster as clauseMasterApi } from '@/api/endpoints';
import { errorMessage } from '@/api/errors';
import type { ClauseCategory } from '@/api/types';
import { Badge } from '@/components/common/Badge';
import { ErrorBanner } from '@/components/common/Banner';
import { Button } from '@/components/common/Button';
import { Card, PageHeader, SectionHeader } from '@/components/common/Card';
import { EmptyState } from '@/components/common/EmptyState';
import { inputClasses, selectClasses, SelectChevron } from '@/components/common/Field';
import { LoadingSpinner } from '@/components/common/LoadingSpinner';
import { formatDateTime, formatPercent, humanise } from '@/lib/format';

export function ClauseMasterPage() {
  const [includeInactive, setIncludeInactive] = useState(false);
  const [selectedKey, setSelectedKey] = useState<string | null>(null);

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ['clause-master', includeInactive],
    queryFn: () => clauseMasterApi.list(includeInactive),
  });

  const categories = data ?? [];
  const selected = categories.find((category) => category.key === selectedKey) ?? categories[0];

  const groups = categories.reduce<Record<string, ClauseCategory[]>>(
    (accumulator, category) => {
      const group = category.group_name ?? 'Other';
      (accumulator[group] ??= []).push(category);
      return accumulator;
    },
    {},
  );

  return (
    <div className="space-y-5">
      <PageHeader
        title="Clause Master"
        subtitle="Configuration, not code. Adding a clause category or changing what makes one mandatory takes effect on the next extraction."
        actions={
          <label className="inline-flex items-center gap-2 text-sm text-slate-700">
            <input
              type="checkbox"
              checked={includeInactive}
              onChange={(event) => setIncludeInactive(event.target.checked)}
              className="h-4 w-4 rounded border-slate-300 text-blue-600 focus:ring-blue-500"
            />
            Show inactive
          </label>
        }
      />

      {error ? (
        <ErrorBanner message={errorMessage(error)} onRetry={() => void refetch()} />
      ) : null}

      {isLoading ? (
        <Card>
          <LoadingSpinner label="Loading clause categories..." />
        </Card>
      ) : categories.length ? (
        <div className="grid gap-5 lg:grid-cols-[18rem_minmax(0,1fr)] lg:items-start">
          <Card
            dense
            className="lg:sticky lg:top-24 lg:max-h-[calc(100vh-8rem)] lg:overflow-y-auto"
          >
            <label className="block lg:hidden">
              <span className="mb-1.5 block text-sm font-medium text-slate-700">Category</span>
              <div className="relative">
              <select
                value={selected?.key ?? ''}
                onChange={(event) => setSelectedKey(event.target.value)}
                className={selectClasses}
              >
                {categories
                  .slice()
                  .sort((a, b) => a.priority - b.priority)
                  .map((category) => (
                    <option key={category.id} value={category.key}>
                      {category.priority}. {category.name}
                      {category.mandatory ? ' (required)' : ''}
                      {category.is_active ? '' : ' — inactive'}
                    </option>
                  ))}
              </select>
              <SelectChevron />
              </div>
            </label>

            <div className="hidden lg:block">
              {Object.entries(groups).map(([group, entries]) => (
                <div key={group} className="mb-4 last:mb-0">
                  <p className="px-2 pb-1.5 text-xs font-semibold uppercase tracking-[0.16em] text-slate-400">
                    {group}
                  </p>
                  <div className="space-y-0.5">
                    {entries
                      .slice()
                      .sort((a, b) => a.priority - b.priority)
                      .map((category) => {
                        const active = selected?.key === category.key;
                        return (
                          <button
                            key={category.id}
                            type="button"
                            onClick={() => setSelectedKey(category.key)}
                            className={[
                              'flex w-full items-center gap-2 rounded-xl px-2.5 py-2 text-left text-sm transition',
                              active
                                ? 'bg-blue-50 text-blue-700 ring-1 ring-blue-100'
                                : 'text-slate-600 hover:bg-slate-100',
                            ].join(' ')}
                          >
                            <span className="w-5 shrink-0 text-xs tabular-nums text-slate-400">
                              {category.priority}
                            </span>
                            <span className="min-w-0 flex-1 truncate">{category.name}</span>
                            {category.mandatory ? (
                              <span
                                title="Absence is treated as a risk"
                                className="shrink-0 rounded bg-blue-100 px-1.5 py-0.5 text-[10px] font-semibold uppercase text-blue-700"
                              >
                                req
                              </span>
                            ) : null}
                            {!category.is_active ? (
                              <span className="shrink-0 rounded bg-slate-200 px-1.5 py-0.5 text-[10px] font-semibold uppercase text-slate-600">
                                off
                              </span>
                            ) : null}
                          </button>
                        );
                      })}
                  </div>
                </div>
              ))}
            </div>
          </Card>

          <div className="min-w-0">
            {selected ? <CategoryDetail key={selected.id} category={selected} /> : null}
          </div>
        </div>
      ) : (
        <EmptyState
          icon={Settings2}
          title="No clause categories"
          description="Run the seed command to install the standard categories, then they appear here ready to configure."
        />
      )}
    </div>
  );
}

function CategoryDetail({ category }: { category: ClauseCategory }) {
  const queryClient = useQueryClient();
  const initial = {
    mandatory: category.mandatory,
    confidence_threshold: category.confidence_threshold,
    is_active: category.is_active,
    priority: category.priority,
  };
  const [draft, setDraft] = useState(initial);
  const [saveError, setSaveError] = useState<string | null>(null);

  const save = useMutation({
    mutationFn: () => clauseMasterApi.update(category.id, draft),
    onSuccess: async () => {
      setSaveError(null);
      await queryClient.invalidateQueries({ queryKey: ['clause-master'] });
    },
    onError: (caught) => setSaveError(errorMessage(caught)),
  });

  const dirty =
    draft.mandatory !== category.mandatory ||
    draft.confidence_threshold !== category.confidence_threshold ||
    draft.is_active !== category.is_active ||
    draft.priority !== category.priority;

  const rule = category.current_rule;
  const uiConfig = category.ui_config ?? {};
  const dropdown = (uiConfig.cap_dropdown ?? uiConfig.dropdown) as
    { field: string; options: string[] } | undefined;

  return (
    <div className="space-y-4">
      <Card>
        <div className="mb-5 flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <h3 className="text-lg font-semibold text-slate-900">{category.name}</h3>
              <span className="rounded-lg bg-slate-100 px-2 py-0.5 font-mono text-xs text-slate-600">
                {category.key}
              </span>
              {category.is_system ? (
                <Badge text="System" variant="neutral" title="Seeded with the platform" />
              ) : null}
            </div>
            {category.description ? (
              <p className="mt-1.5 text-sm leading-6 text-slate-500">{category.description}</p>
            ) : null}
          </div>

          {dirty ? (
            <div className="flex shrink-0 gap-2">
              <Button variant="secondary" size="sm" onClick={() => setDraft(initial)}>
                Discard
              </Button>
              <Button size="sm" busy={save.isPending} onClick={() => save.mutate()}>
                {save.isPending ? 'Saving…' : 'Save'}
              </Button>
            </div>
          ) : null}
        </div>

        {saveError ? (
          <div className="mb-4">
            <ErrorBanner message={saveError} />
          </div>
        ) : null}

        <div className="grid gap-5 sm:grid-cols-2">
          <label className="flex gap-3">
            <input
              type="checkbox"
              checked={draft.mandatory}
              onChange={(event) => setDraft({ ...draft, mandatory: event.target.checked })}
              className="mt-0.5 h-4 w-4 shrink-0 rounded border-slate-300 text-blue-600 focus:ring-blue-500"
            />
            <span>
              <span className="block text-sm font-medium text-slate-800">Mandatory</span>
              <span className="mt-0.5 block text-xs leading-5 text-slate-500">
                When absent from a contract, its absence is recorded as a risk and contributes
                to the risk score.
              </span>
            </span>
          </label>

          <label className="flex gap-3">
            <input
              type="checkbox"
              checked={draft.is_active}
              onChange={(event) => setDraft({ ...draft, is_active: event.target.checked })}
              className="mt-0.5 h-4 w-4 shrink-0 rounded border-slate-300 text-blue-600 focus:ring-blue-500"
            />
            <span>
              <span className="block text-sm font-medium text-slate-800">Active</span>
              <span className="mt-0.5 block text-xs leading-5 text-slate-500">
                Inactive categories are skipped by future extractions. Existing extractions are
                unaffected.
              </span>
            </span>
          </label>

          <div>
            <p className="text-sm font-medium text-slate-800">Confidence threshold</p>
            <div className="mt-2 flex items-center gap-3">
              <input
                type="range"
                min={0.5}
                max={0.99}
                step={0.01}
                value={draft.confidence_threshold}
                onChange={(event) =>
                  setDraft({ ...draft, confidence_threshold: Number(event.target.value) })
                }
                className="h-2 flex-1 cursor-pointer appearance-none rounded-full bg-slate-200 accent-blue-600"
              />
              <span className="w-12 text-right font-mono text-sm tabular-nums text-slate-700">
                {formatPercent(draft.confidence_threshold)}
              </span>
            </div>
            <p className="mt-1.5 text-xs leading-5 text-slate-500">
              Extractions below this are routed to human review rather than accepted.
            </p>
          </div>

          <div>
            <p className="text-sm font-medium text-slate-800">Priority</p>
            <input
              type="number"
              min={1}
              value={draft.priority}
              onChange={(event) => setDraft({ ...draft, priority: Number(event.target.value) })}
              className={`${inputClasses} mt-2 w-28`}
            />
            <p className="mt-1.5 text-xs leading-5 text-slate-500">
              Orders the clause tabs on the contract screen.
            </p>
          </div>
        </div>
      </Card>

      <Card>
        <SectionHeader
          title="Presentation"
          subtitle="How this category appears on the contract screen."
        />
        <dl className="grid gap-3 sm:grid-cols-2">
          <div className="rounded-xl bg-slate-50 px-3 py-2.5">
            <dt className="text-xs font-semibold uppercase tracking-wider text-slate-400">
              Placement
            </dt>
            <dd className="mt-1 text-sm text-slate-900">
              {humanise(String(uiConfig.placement ?? 'list'))}
            </dd>
          </div>
          <div className="rounded-xl bg-slate-50 px-3 py-2.5">
            <dt className="text-xs font-semibold uppercase tracking-wider text-slate-400">
              Tab label
            </dt>
            <dd className="mt-1 text-sm text-slate-900">
              {String(uiConfig.tab_label ?? category.name)}
            </dd>
          </div>
        </dl>

        {Array.isArray(uiConfig.primary_fields) && uiConfig.primary_fields.length ? (
          <ChipList
            label="Leading fields"
            values={(uiConfig.primary_fields as string[]).map(humanise)}
          />
        ) : null}

        {dropdown ? (
          <ChipList
            label={`Dropdown — ${humanise(dropdown.field)}`}
            values={dropdown.options.map(humanise)}
          />
        ) : null}

        {uiConfig.highlight_when != null &&
        Object.keys(uiConfig.highlight_when as object).length ? (
          <ChipList
            label="Flagged when"
            values={Object.entries(uiConfig.highlight_when as Record<string, unknown>).map(
              ([field, value]) => `${humanise(field)} = ${String(value)}`,
            )}
          />
        ) : null}
      </Card>

      {rule ? (
        <Card>
          <div className="mb-4 flex flex-wrap items-center gap-2">
            <h3 className="text-lg font-semibold text-slate-900">Extraction rule</h3>
            <span className="rounded-lg bg-slate-100 px-2 py-0.5 font-mono text-xs text-slate-600">
              v{rule.version}
            </span>
          </div>
          <p className="text-xs leading-5 text-slate-500">
            Created {formatDateTime(rule.created_at)}
            {rule.change_note ? ` — ${rule.change_note}` : ''}. Rules are versioned, so any
            extraction can be explained by the rule that produced it.
          </p>

          {rule.synonyms.length ? (
            <ChipList label="Also known as" values={rule.synonyms} />
          ) : null}

          <details className="mt-4 group">
            <summary className="cursor-pointer text-sm font-medium text-blue-600 hover:text-blue-700">
              Matching rule
            </summary>
            <pre className="mt-2 overflow-x-auto rounded-xl bg-slate-900 p-4 font-mono text-xs leading-relaxed text-slate-100">
              {JSON.stringify(rule.extraction_rule, null, 2)}
            </pre>
          </details>

          <details className="mt-2 group">
            <summary className="cursor-pointer text-sm font-medium text-blue-600 hover:text-blue-700">
              Output schema
            </summary>
            <pre className="mt-2 overflow-x-auto rounded-xl bg-slate-900 p-4 font-mono text-xs leading-relaxed text-slate-100">
              {JSON.stringify(rule.output_schema, null, 2)}
            </pre>
          </details>
        </Card>
      ) : null}
    </div>
  );
}

const ChipList = ({ label, values }: { label: string; values: string[] }) => (
  <div className="mt-4">
    <p className="mb-2 text-xs font-semibold uppercase tracking-wider text-slate-400">
      {label}
    </p>
    <div className="flex flex-wrap gap-2">
      {values.map((value) => (
        <span
          key={value}
          className="rounded-full bg-slate-100 px-3 py-1 text-xs font-medium text-slate-600"
        >
          {value}
        </span>
      ))}
    </div>
  </div>
);

export default ClauseMasterPage;
