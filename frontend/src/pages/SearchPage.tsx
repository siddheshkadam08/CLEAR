/**
 * Search.
 *
 * Two things distinguish this from a search box. First, the retrieval plan is
 * visible: what the planner decided the question meant, which strategy it chose and
 * which filters it applied. Second, an empty result shows that plan rather than a
 * shrug - "no results" is usually a misread question, and the reasoning is what lets
 * the user correct it.
 */

import { useMutation } from '@tanstack/react-query';
import { Route, SearchX, Search as SearchIcon, Sparkles } from 'lucide-react';
import { useState } from 'react';
import { useNavigate } from 'react-router-dom';

import { search as searchApi } from '@/api/endpoints';
import { errorMessage } from '@/api/errors';
import type { PlanExplanation, SearchResponse } from '@/api/types';
import { Badge } from '@/components/common/Badge';
import { getRiskVariant } from '@/lib/badges';
import { ErrorBanner, NoticeBanner } from '@/components/common/Banner';
import { Button } from '@/components/common/Button';
import { Card, PageHeader, SectionHeader } from '@/components/common/Card';
import { EmptyState } from '@/components/common/EmptyState';
import { inputClasses, selectClasses, SelectChevron } from '@/components/common/Field';
import { LoadingSpinner } from '@/components/common/LoadingSpinner';
import { formatAgreementType, formatDate, formatDuration, humanise } from '@/lib/format';
import { useProjectScope } from '@/lib/scope';

const EXAMPLES = [
  'Which contracts have uncapped liability?',
  'Show payment terms longer than 60 days',
  'Contracts expiring in the next quarter with auto-renewal',
  'Where does the counterparty hold termination rights for convenience?',
];

export function SearchPage() {
  const { projectId } = useProjectScope();
  const navigate = useNavigate();
  const [query, setQuery] = useState('');
  const [mode, setMode] = useState('hybrid');
  const [result, setResult] = useState<SearchResponse | null>(null);
  const [showPlan, setShowPlan] = useState(false);

  const run = useMutation({
    mutationFn: (text: string) =>
      searchApi.query({ query: text, project_id: projectId, mode, limit: 30 }),
    onSuccess: setResult,
  });

  function submit(text: string) {
    const trimmed = text.trim();
    if (!trimmed) return;
    setQuery(trimmed);
    run.mutate(trimmed);
  }

  return (
    <div className="space-y-5">
      <PageHeader
        title="Search"
        subtitle="Ask in plain language, or search for exact wording. Results are scoped to the projects you belong to."
      />

      <Card>
        <form
          className="flex flex-col gap-3 sm:flex-row"
          onSubmit={(event) => {
            event.preventDefault();
            submit(query);
          }}
        >
          <div className="relative flex-1">
            <SearchIcon className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-400" />
            <input
              type="search"
              placeholder="Search contracts, clauses and sections"
              aria-label="Search query"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              className={`${inputClasses} pl-9`}
            />
          </div>
          <div className="relative sm:w-40">
          <select
            value={mode}
            onChange={(event) => setMode(event.target.value)}
            aria-label="Search mode"
            title="Hybrid fuses meaning and exact wording. Keyword finds literal phrases; semantic finds paraphrases."
            className={selectClasses}
          >
            <option value="hybrid">Hybrid</option>
            <option value="semantic">Semantic</option>
            <option value="keyword">Keyword</option>
          </select>
          <SelectChevron />
          </div>
          <Button type="submit" busy={run.isPending} icon={SearchIcon} className="sm:w-auto">
            {run.isPending ? 'Searching…' : 'Search'}
          </Button>
        </form>

        {!result && !run.isPending ? (
          <div className="mt-4">
            <p className="mb-2 text-xs font-semibold uppercase tracking-wider text-slate-400">
              Try one of these
            </p>
            <div className="flex flex-wrap gap-2">
              {EXAMPLES.map((example) => (
                <button
                  key={example}
                  type="button"
                  onClick={() => submit(example)}
                  className="rounded-full bg-slate-100 px-3 py-1.5 text-left text-xs font-medium text-slate-600 transition hover:bg-blue-50 hover:text-blue-700"
                >
                  {example}
                </button>
              ))}
            </div>
          </div>
        ) : null}
      </Card>

      {run.error ? (
        <ErrorBanner message={errorMessage(run.error)} onRetry={() => submit(query)} />
      ) : null}

      {run.isPending ? (
        <Card>
          <LoadingSpinner label="Searching the repository..." />
        </Card>
      ) : result ? (
        <div className="space-y-4">
          <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
            <span className="text-sm text-slate-500">
              {result.total_hits} passage{result.total_hits === 1 ? '' : 's'} in{' '}
              {result.contracts.length} contract{result.contracts.length === 1 ? '' : 's'} ·{' '}
              {formatDuration(result.duration_ms)}
            </span>
            <button
              type="button"
              onClick={() => setShowPlan((open) => !open)}
              className="self-start text-sm font-medium text-blue-600 transition hover:text-blue-700"
            >
              {showPlan ? 'Hide' : 'Show'} how this was searched
            </button>
          </div>

          {result.warnings.length ? (
            <NoticeBanner message={result.warnings.join(' · ')} />
          ) : null}

          {showPlan ? <PlanCard plan={result.plan} /> : null}

          {result.hits.length === 0 ? (
            <div className="space-y-4">
              <EmptyState
                icon={SearchX}
                title="Nothing matched"
                description="The retrieval plan shows how the question was interpreted. If the intent or the filters look wrong, rephrasing usually fixes it — or switch to keyword mode to search for exact wording."
              />
              <PlanCard plan={result.plan} />
            </div>
          ) : (
            <>
              {result.contracts.length ? (
                <Card className="overflow-hidden">
                  <SectionHeader
                    title="Matching contracts"
                    subtitle="Documents containing at least one matching passage."
                    icon={Sparkles}
                  />
                  <ul className="divide-y divide-slate-100">
                    {result.contracts.map((match) => (
                      <li key={match.contract_id}>
                        <button
                          type="button"
                          onClick={() => navigate(`/contracts/${match.contract_id}`)}
                          className="flex w-full flex-col gap-2 py-3 text-left transition hover:bg-slate-50 sm:flex-row sm:items-center sm:justify-between"
                        >
                          <div className="min-w-0">
                            <p className="truncate text-sm font-medium text-slate-900">
                              {match.title ?? 'Untitled'}
                            </p>
                            <p className="mt-0.5 text-xs text-slate-500">
                              {formatAgreementType(match.agreement_type)} ·{' '}
                              {formatDate(match.expiration_date)}
                            </p>
                          </div>
                          <div className="flex shrink-0 flex-wrap items-center gap-2">
                            {match.has_unlimited_liability ? (
                              <Badge text="Unlimited liability" variant="danger" />
                            ) : null}
                            <Badge
                              text={match.risk_band ? humanise(match.risk_band) : '—'}
                              variant={getRiskVariant(match.risk_band)}
                            />
                          </div>
                        </button>
                      </li>
                    ))}
                  </ul>
                </Card>
              ) : null}

              <div className="space-y-3">
                {result.hits.map((hit) => (
                  <button
                    key={`${hit.level}:${hit.ref_id}`}
                    type="button"
                    onClick={() => navigate(`/contracts/${hit.contract_id}`)}
                    className="block w-full rounded-2xl border border-slate-200 bg-white p-4 text-left shadow-sm transition hover:border-blue-200 hover:shadow-md sm:p-5"
                  >
                    <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
                      <div className="flex min-w-0 flex-wrap items-center gap-2">
                        <span className="truncate text-sm font-semibold text-slate-900">
                          {hit.contract_title ?? 'Untitled'}
                        </span>
                        {hit.clause_number ? (
                          <Badge text={hit.clause_number} variant="neutral" />
                        ) : null}
                        {hit.clause_type ? (
                          <Badge text={humanise(hit.clause_type)} variant="info" />
                        ) : null}
                      </div>
                      <div className="flex shrink-0 items-center gap-3 text-xs text-slate-500">
                        {/* The retrieval source is shown because it explains why a
                            result is here: a neighbour was pulled in for context, a
                            graph hop connected it, a keyword matched literally. */}
                        <span title="How this passage was retrieved">{hit.source}</span>
                        <span className="font-mono tabular-nums">{hit.score.toFixed(3)}</span>
                        {hit.page_start ? <span>p{hit.page_start}</span> : null}
                      </div>
                    </div>

                    {hit.section_title ? (
                      <p className="mt-1 text-xs text-slate-500">{hit.section_title}</p>
                    ) : null}
                    <p className="mt-2 line-clamp-4 text-sm leading-6 text-slate-700">
                      {hit.text}
                    </p>
                  </button>
                ))}
              </div>
            </>
          )}
        </div>
      ) : null}
    </div>
  );
}

// =============================================================================
// Plan
// =============================================================================
function PlanCard({ plan }: { plan: PlanExplanation }) {
  return (
    <Card>
      <SectionHeader
        title="Retrieval plan"
        subtitle="How the question was interpreted and searched."
        icon={Route}
      />

      <dl className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        {[
          ['Intent', plan.intent],
          ['Strategy', plan.strategy],
          ['Scope', plan.scope],
          ['Mode', plan.mode],
        ].map(([label, value]) => (
          <div key={label} className="rounded-xl bg-slate-50 px-3 py-2.5">
            <dt className="text-xs font-semibold uppercase tracking-wider text-slate-400">
              {label}
            </dt>
            <dd className="mt-1 text-sm font-medium text-slate-900">{humanise(value)}</dd>
          </div>
        ))}
      </dl>

      {Object.keys(plan.filters).length ? (
        <div className="mt-4">
          <p className="mb-2 text-xs font-semibold uppercase tracking-wider text-slate-400">
            Filters applied
          </p>
          <div className="flex flex-wrap gap-2">
            {Object.entries(plan.filters).map(([key, value]) => (
              <Badge
                key={key}
                text={`${humanise(key)}: ${Array.isArray(value) ? value.join(', ') : String(value)}`}
                variant="neutral"
              />
            ))}
          </div>
        </div>
      ) : null}

      {plan.levels.length ? (
        <div className="mt-4">
          <p className="mb-2 text-xs font-semibold uppercase tracking-wider text-slate-400">
            Levels searched
          </p>
          <div className="flex flex-wrap gap-2">
            {plan.levels.map((level) => (
              <Badge
                key={level.level}
                text={`${humanise(level.level)} · top ${level.limit} · ≥${level.min_similarity.toFixed(2)}`}
                variant="neutral"
              />
            ))}
          </div>
        </div>
      ) : null}

      {plan.reasoning.length ? (
        <div className="mt-4">
          <p className="mb-2 text-xs font-semibold uppercase tracking-wider text-slate-400">
            Reasoning
          </p>
          <ul className="space-y-1 text-sm leading-6 text-slate-600">
            {plan.reasoning.map((line, index) => (
              <li key={index} className="flex gap-2">
                <span className="mt-2 h-1 w-1 shrink-0 rounded-full bg-slate-400" />
                <span>{line}</span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </Card>
  );
}

export default SearchPage;
