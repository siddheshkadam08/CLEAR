/**
 * Retrieval quality dashboard.
 *
 * Reads what a benchmark run already wrote. It cannot start one: a run takes
 * minutes and costs money, and a button that spent the budget on a page refresh
 * would be a mistake waiting to happen.
 *
 * The page is organised around one question - *is retrieval getting better or
 * worse* - so the trend and the gate verdict come first, and the failing-case
 * lists come last because they are what you open when the answer is "worse".
 */

import { useQuery } from '@tanstack/react-query';
import { Activity, AlertTriangle, CheckCircle2, TrendingDown, TrendingUp } from 'lucide-react';

import { evaluation as evaluationApi } from '@/api/endpoints';
import { ApiError, errorMessage } from '@/api/errors';
import type { EvaluationLatest, EvaluationRun } from '@/api/types';
import { Badge } from '@/components/common/Badge';
import { ErrorBanner, NoticeBanner } from '@/components/common/Banner';
import { Card, PageHeader } from '@/components/common/Card';
import { EmptyState } from '@/components/common/EmptyState';
import { LoadingSpinner } from '@/components/common/LoadingSpinner';

export function EvaluationPage() {
  const runsQuery = useQuery({
    queryKey: ['evaluation-runs'],
    queryFn: () => evaluationApi.runs(),
  });
  const latestQuery = useQuery({
    queryKey: ['evaluation-latest'],
    queryFn: () => evaluationApi.latest(),
    retry: false,
  });

  if (runsQuery.isLoading || latestQuery.isLoading) {
    return <LoadingSpinner />;
  }

  const runs = runsQuery.data?.runs ?? [];
  const latest = latestQuery.data;

  // A 404 from `latest` is the expected shape of "no runs yet", not a failure.
  // Anything else is: a 403, a 500 and an unreachable API all used to fall
  // through to the empty state below, so "the backend is down" was indis-
  // tinguishable from "nobody has run the benchmark" - the one message that
  // makes you stop looking.
  const failure = [runsQuery.error, latestQuery.error].find(
    (error) => error && !(error instanceof ApiError && error.isNotFound),
  );

  if (failure) {
    return (
      <div className="space-y-5">
        <PageHeader subtitle="Benchmark results for the Copilot's retrieval and answering pipeline." />
        <ErrorBanner message={errorMessage(failure)} onRetry={() => void runsQuery.refetch()} />
      </div>
    );
  }

  if (!latest || !runs.length) {
    return (
      <div className="space-y-5">
        <PageHeader subtitle="Benchmark results for the Copilot's retrieval and answering pipeline." />
        <EmptyState
          icon={Activity}
          title="No benchmark has been recorded"
          description="Run `python -m app.evaluation.cli benchmark --dataset <name>`. It writes to EVALUATION_RESULTS_DIR, which is the directory this screen reads. Until then there is nothing to compare against, and every threshold in the pipeline is unmeasured."
        />
      </div>
    );
  }

  const metrics = latest.summary.metrics;
  // The run before the newest one, for the per-tile delta. Absent on the first
  // recorded run, in which case no tile shows a change - which is honest, rather
  // than comparing against zero.
  const previous = runs.at(-2)?.metrics;

  return (
    <div className="space-y-5">
      {/* Run metadata moved into the gate banner below, where it labels the
          verdict it belongs to. As the page's only description it read as an
          orphaned breadcrumb. */}
      <PageHeader subtitle="Benchmark results for the Copilot's retrieval and answering pipeline." />

      <div
        className={[
          'flex items-start gap-3 rounded-2xl border px-5 py-4',
          latest.summary.passed
            ? 'border-emerald-200 bg-emerald-50 text-emerald-800'
            : 'border-rose-200 bg-rose-50 text-rose-800',
        ].join(' ')}
      >
        {latest.summary.passed ? (
          <CheckCircle2 className="mt-0.5 h-5 w-5 shrink-0" />
        ) : (
          <AlertTriangle className="mt-0.5 h-5 w-5 shrink-0" />
        )}
        <div>
          <p className="font-semibold">{latest.summary.summary}</p>
          <p className="mt-1 text-sm opacity-80">
            {latest.summary.dataset} · {latest.summary.cases} cases · {latest.summary.label}
          </p>
          {latest.summary.failures ? (
            <p className="mt-1 text-sm">
              {latest.summary.failures} case(s) failed to execute and were excluded from every
              quality metric.
            </p>
          ) : null}
        </div>
      </div>

      <section className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Tile label="Composite" value={fmt(metrics.composite)} delta={delta(metrics, previous, 'composite')} />
        <Tile label="Recall@10" value={fmt(metrics['recall@10'])} delta={delta(metrics, previous, 'recall@10')} />
        <Tile label="MRR" value={fmt(metrics.mrr)} delta={delta(metrics, previous, 'mrr')} />
        <Tile
          label="Citation precision"
          value={fmt(metrics.citation_precision)}
          delta={delta(metrics, previous, 'citation_precision')}
        />
        <Tile
          label="Hallucination rate"
          value={fmt(metrics.hallucination_rate)}
          delta={delta(metrics, previous, 'hallucination_rate')}
          lowerIsBetter
        />
        <Tile
          label="Guardrail accuracy"
          value={fmt(metrics.guardrail_accuracy)}
          delta={delta(metrics, previous, 'guardrail_accuracy')}
        />
        <Tile
          label="p95 latency"
          value={`${Math.round(metrics.latency_p95_ms ?? 0)} ms`}
          delta={delta(metrics, previous, 'latency_p95_ms')}
          lowerIsBetter
        />
        <Tile
          label="Cost / query"
          value={`$${(metrics.mean_cost_usd ?? 0).toFixed(5)}`}
          delta={delta(metrics, previous, 'mean_cost_usd')}
          lowerIsBetter
        />
      </section>

      {latest.calibration ? (
        <Card>
          <SectionTitle>Confidence calibration</SectionTitle>
          <div className="grid gap-3 sm:grid-cols-4">
            <Stat label="ECE" value={fmt(latest.calibration.raw.ece)} hint="lower is better" />
            <Stat label="MCE" value={fmt(latest.calibration.raw.mce)} hint="worst bin" />
            <Stat label="Brier" value={fmt(latest.calibration.raw.brier)} />
            <Stat
              label="Bias"
              value={signed(latest.calibration.raw.mean_bias)}
              hint="positive = over-confident"
            />
          </div>
          <p className="mt-3 text-sm text-slate-600">{latest.calibration.recommendation}</p>
          <p className="mt-2 text-xs text-slate-500">
            Calibrators are fitted and scored only. Runtime confidence is unchanged — a figure that
            silently changed meaning between releases would be worse than one never calibrated.
          </p>
        </Card>
      ) : null}

      {latest.guardrail ? (
        <Card>
          <SectionTitle>Guardrail</SectionTitle>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-xs uppercase tracking-[0.06em] text-slate-500">
                  <th className="py-2 text-left" />
                  <th className="py-2 text-right">Answered</th>
                  <th className="py-2 text-right">Declined</th>
                </tr>
              </thead>
              <tbody>
                <tr className="border-t border-slate-100">
                  <td className="py-2 font-medium">Should answer</td>
                  <td className="py-2 text-right">
                    <Badge text={String(latest.guardrail.true_accept)} variant="success" />
                  </td>
                  <td className="py-2 text-right">
                    <Badge text={String(latest.guardrail.false_reject)} variant="warning" />
                  </td>
                </tr>
                <tr className="border-t border-slate-100">
                  <td className="py-2 font-medium">Should decline</td>
                  <td className="py-2 text-right">
                    <Badge text={String(latest.guardrail.false_accept)} variant="danger" />
                  </td>
                  <td className="py-2 text-right">
                    <Badge text={String(latest.guardrail.true_reject)} variant="success" />
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
          <p className="mt-3 text-xs text-slate-500">
            False accept is the dangerous quadrant: the platform produced contract terms for a
            question the corpus cannot support. False reject is costly but safe.
          </p>
          {latest.guardrail.document_summary_would_have_passed > 0 ? (
            <div className="mt-3">
              <NoticeBanner
                message={`A document summary cleared the answer threshold while no clause did in ${latest.guardrail.document_summary_would_have_passed} case(s); ${latest.guardrail.hallucinations_prevented} of those were genuinely unanswerable. Each would have passed a guardrail computed on the whole-result maximum.`}
              />
            </div>
          ) : null}
        </Card>
      ) : null}

      {Object.keys(latest.by_tag ?? {}).length ? (
        <Card>
          <SectionTitle>By segment</SectionTitle>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-xs uppercase tracking-[0.06em] text-slate-500">
                  <th className="py-2 text-left">Tag</th>
                  <th className="py-2 text-right">Cases</th>
                  <th className="py-2 text-right">Recall@10</th>
                  <th className="py-2 text-right">MRR</th>
                  <th className="py-2 text-right">Citation precision</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(latest.by_tag ?? {}).map(([tag, values]) => (
                  <tr key={tag} className="border-t border-slate-100">
                    <td className="py-2">{tag}</td>
                    <td className="py-2 text-right tabular-nums">{values.cases}</td>
                    <td className="py-2 text-right tabular-nums">{fmt(values['recall@10'])}</td>
                    <td className="py-2 text-right tabular-nums">{fmt(values.mrr)}</td>
                    <td className="py-2 text-right tabular-nums">
                      {fmt(values.citation_precision)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="mt-3 text-xs text-slate-500">
            Segments with fewer than three cases are omitted — an average over two is noise
            presented as a trend.
          </p>
        </Card>
      ) : null}

      <FailingCases latest={latest} />

      <Card>
        <SectionTitle>Run history</SectionTitle>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-xs uppercase tracking-[0.06em] text-slate-500">
                <th className="py-2 text-left">Run</th>
                <th className="py-2 text-right">Cases</th>
                <th className="py-2 text-right">Composite</th>
                <th className="py-2 text-right">Recall@10</th>
                <th className="py-2 text-right">Citation precision</th>
                <th className="py-2 text-left">Gate</th>
              </tr>
            </thead>
            <tbody>
              {[...runs].reverse().map((run: EvaluationRun) => (
                <tr key={run.label} className="border-t border-slate-100">
                  <td className="py-2 font-mono text-xs">{run.label}</td>
                  <td className="py-2 text-right tabular-nums">{run.cases}</td>
                  <td className="py-2 text-right tabular-nums">{fmt(run.metrics.composite)}</td>
                  <td className="py-2 text-right tabular-nums">{fmt(run.metrics['recall@10'])}</td>
                  <td className="py-2 text-right tabular-nums">
                    {fmt(run.metrics.citation_precision)}
                  </td>
                  <td className="py-2">
                    <Badge
                      text={run.passed ? 'pass' : 'fail'}
                      variant={run.passed ? 'success' : 'danger'}
                    />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>
    </div>
  );
}

// =============================================================================
// Pieces
// =============================================================================
function FailingCases({ latest }: { latest: EvaluationLatest }) {
  const sections: Array<{ title: string; hint: string; key: keyof EvaluationLatest['failing'] }> = [
    {
      title: 'Retrieved nothing relevant',
      hint: 'A wrong expectation, a missing document, or a genuine retrieval failure — distinguishable only by looking.',
      key: 'zero_recall',
    },
    {
      title: 'Answered when it should have declined',
      hint: 'The dangerous quadrant.',
      key: 'false_accept',
    },
    {
      title: 'Declined when it should have answered',
      hint: 'Costly but safe.',
      key: 'false_reject',
    },
    { title: 'Worst-cited answers', hint: 'Fabricated, broken or imprecise citations.', key: 'worst_cited' },
    {
      title: 'Filtered to the wrong document type',
      hint: 'A type filter was applied and nothing expected came back.',
      key: 'false_filtering',
    },
    { title: 'Most expensive', hint: 'Where a cost regression is diagnosed.', key: 'most_expensive' },
  ];

  const populated = sections.filter((section) => (latest.failing[section.key] ?? []).length);
  if (!populated.length) return null;

  return (
    <Card>
      <SectionTitle>Failing cases</SectionTitle>
      <div className="space-y-5">
        {populated.map((section) => (
          <div key={section.key}>
            <p className="text-sm font-medium text-slate-700">
              {section.title}{' '}
              <span className="text-slate-400">({latest.failing[section.key].length})</span>
            </p>
            <p className="mt-0.5 text-xs text-slate-500">{section.hint}</p>
            <ul className="mt-2 space-y-1">
              {latest.failing[section.key].map((entry) => (
                <li key={entry.id} className="text-sm text-slate-600">
                  <code className="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-xs">
                    {entry.id}
                  </code>{' '}
                  {entry.question}
                </li>
              ))}
            </ul>
          </div>
        ))}
      </div>
    </Card>
  );
}

function Tile({
  label,
  value,
  delta: change,
  lowerIsBetter = false,
}: {
  label: string;
  value: string;
  delta?: number;
  lowerIsBetter?: boolean;
}) {
  // Colour follows goodness, not sign: a fall in hallucination rate is an
  // improvement even though the arrow points down.
  const improved = change === undefined ? null : lowerIsBetter ? change < 0 : change > 0;
  const meaningful = change !== undefined && Math.abs(change) > 1e-6;

  return (
    <div className="rounded-2xl border border-slate-200 bg-white p-4 dark:border-slate-700 dark:bg-slate-800">
      <p className="text-xs font-semibold uppercase tracking-[0.08em] text-slate-500">{label}</p>
      <p className="mt-1 text-2xl font-semibold tabular-nums text-slate-900 dark:text-slate-100">{value}</p>
      {meaningful ? (
        <p
          className={[
            'mt-1 flex items-center gap-1 text-xs tabular-nums',
            improved ? 'text-emerald-600' : 'text-rose-600',
          ].join(' ')}
        >
          {improved ? <TrendingUp className="h-3 w-3" /> : <TrendingDown className="h-3 w-3" />}
          {change > 0 ? '+' : ''}
          {change.toFixed(4)} vs previous
        </p>
      ) : null}
    </div>
  );
}

function Stat({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div>
      <p className="text-xs font-semibold uppercase tracking-[0.08em] text-slate-500">{label}</p>
      <p className="mt-1 text-lg font-semibold tabular-nums text-slate-900 dark:text-slate-100">{value}</p>
      {hint ? <p className="text-xs text-slate-500">{hint}</p> : null}
    </div>
  );
}

function SectionTitle({ children }: { children: React.ReactNode }) {
  return <h2 className="mb-3 text-sm font-semibold text-slate-700">{children}</h2>;
}

function fmt(value: number | undefined): string {
  return value === undefined ? '—' : value.toFixed(3);
}

function signed(value: number | undefined): string {
  return value === undefined ? '—' : `${value > 0 ? '+' : ''}${value.toFixed(3)}`;
}

function delta(
  current: Record<string, number>,
  previous: Record<string, number> | undefined,
  key: string,
): number | undefined {
  if (!previous || previous[key] === undefined || current[key] === undefined) return undefined;
  return current[key] - previous[key];
}

export default EvaluationPage;
