/**
 * Clause coverage.
 *
 * Coverage is the figure to read. A raw clause count says little on its own -
 * eleven clauses is complete for an NDA and two-thirds of an MSA - so every count
 * is shown against what the document type's profile says it should have.
 *
 * Was "Document pipeline", and claimed to read `cip_DocMaster` /
 * `cip_DocContentMaster` "separate from the contract pipeline's own counts".
 * Both halves stopped being true when those tables were decommissioned: the
 * endpoint now reads `contracts`, `clauses`, `embeddings` and `document_profiles`
 * - the same tables the dashboard counts. There is one definition of "clause".
 *
 * Every list here is bounded and scrolls inside its card. `missing_clauses` is
 * uncapped server-side (it is *every* expected clause not found, per processed
 * type, which is types x ~30), so without a ceiling one card grew to several
 * thousand pixels and stretched the chart beside it.
 */

import { useQuery } from '@tanstack/react-query';
import {
  FileSearch,
  FileStack,
  Layers,
  ListChecks,
  SearchX,
  Sparkles,
} from 'lucide-react';
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';

import { docpipeline as api } from '@/api/endpoints';
import { errorMessage } from '@/api/errors';
import { Badge } from '@/components/common/Badge';
import { ErrorBanner } from '@/components/common/Banner';
import { ACCENTS, Card, KpiSkeleton, MetricCard, PageHeader, SectionHeader } from '@/components/common/Card';
import { EmptyState } from '@/components/common/EmptyState';
import { useProjectScope } from '@/lib/scope';

/**
 * One description, used by every branch.
 *
 * The loading, empty and populated states used to carry three different
 * sentences, so the line describing the screen changed as data arrived.
 */
const SUBTITLE =
  'How much of each document type’s expected clause set was actually found, and which clauses were never located at all.';

const percent = (value: number | null | undefined) =>
  value === null || value === undefined ? '--' : `${Math.round(value * 100)}%`;

/** Green at full coverage, amber past half, red below. */
const coverageVariant = (value: number | null | undefined) => {
  if (value === null || value === undefined) return 'neutral' as const;
  if (value >= 0.9) return 'success' as const;
  if (value >= 0.6) return 'warning' as const;
  return 'danger' as const;
};

/** Last path segment, so a row reads as a document rather than a directory. */
const fileNameOf = (path: string | null | undefined) => {
  if (!path) return null;
  const name = path.split(/[\\/]/).pop();
  return name && name.length ? name : null;
};

export const ClauseCoveragePage = () => {
  // Every other screen narrows to the selected business unit; this one used to
  // ignore it and always show the union of every project the caller belongs to.
  // The endpoint has always accepted `project_id`.
  const { projectId } = useProjectScope();
  const { data, isLoading, error } = useQuery({
    queryKey: ['docpipeline', projectId],
    queryFn: () => api.insights(projectId),
  });

  if (error) return <ErrorBanner message={errorMessage(error)} />;

  if (isLoading || !data) {
    return (
      <div className="space-y-5">
        <PageHeader subtitle={SUBTITLE} />
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          {[0, 1, 2, 3].map((i) => (
            <KpiSkeleton key={i} />
          ))}
        </div>
      </div>
    );
  }

  const { totals, documents, by_doc_type, clause_frequency, missing_clauses } = data;

  if (totals.documents === 0) {
    return (
      <div className="space-y-5">
        <PageHeader subtitle={SUBTITLE} />
        <Card>
          <EmptyState
            icon={FileSearch}
            title="No documents processed yet"
            description="Upload a contract and let it finish processing; its clauses and coverage appear here."
          />
        </Card>
      </div>
    );
  }

  return (
    <div className="space-y-5">
      <PageHeader subtitle={SUBTITLE} />

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <MetricCard
          label="Documents"
          value={String(totals.documents)}
          hint="processed and recorded"
          icon={FileStack}
          accent={ACCENTS[0]}
        />
        <MetricCard
          label="Clauses located"
          value={String(totals.clauses)}
          hint={`${totals.clauses_per_document} per document on average`}
          icon={ListChecks}
          accent={ACCENTS[1]}
        />
        <MetricCard
          label="Average coverage"
          value={percent(totals.average_coverage)}
          hint="of the clauses the taxonomy expects"
          icon={Sparkles}
          accent={ACCENTS[2]}
        />
        <MetricCard
          label="Embedded"
          value={`${totals.embedded} / ${totals.clauses}`}
          hint={`${totals.clause_page_regions} page regions to highlight`}
          icon={Layers}
          accent={ACCENTS[5]}
        />
      </div>

      <Card>
        <SectionHeader
          title="Documents"
          subtitle="Clauses found against the number the document type should have."
          icon={FileStack}
          action={
            <span className="text-xs tabular-nums text-slate-500 dark:text-slate-400">
              {documents.length} shown
            </span>
          }
        />
        {/* Scrolls in both directions: the header stays put so a coverage figure
            near the bottom still has a column name. */}
        <div className="max-h-[26rem] overflow-auto">
          <table className="w-full text-sm">
            <thead className="sticky top-0 z-10 bg-white dark:bg-slate-800">
              <tr className="border-b border-slate-200 dark:border-slate-700 text-left text-xs uppercase tracking-wide text-slate-500 dark:text-slate-400">
                <th scope="col" className="pb-2 pr-4">Document</th>
                <th scope="col" className="pb-2 pr-4">Type</th>
                <th scope="col" className="pb-2 pr-4">Clauses</th>
                <th scope="col" className="pb-2">Coverage</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100 dark:divide-slate-700">
              {documents.map((doc) => {
                const source = doc.doc_path ?? doc.json_path ?? null;
                return (
                  <tr key={doc.docid}>
                    {/* Was `#{docid}`, which printed a raw UUID once the endpoint
                        stopped returning integer ids. */}
                    <td
                      className="max-w-xs truncate py-3 pr-4 font-medium text-slate-900 dark:text-slate-100"
                      title={source ?? undefined}
                    >
                      {fileNameOf(source) ?? 'Untitled document'}
                    </td>
                    <td className="py-3 pr-4">
                      <Badge variant="neutral" text={doc.doc_type ?? 'unknown'} />
                    </td>
                    <td className="py-3 pr-4 tabular-nums text-slate-700 dark:text-slate-300">
                      {doc.clauses_found} / {doc.clauses_expected}
                    </td>
                    <td className="py-3">
                      <Badge
                        variant={coverageVariant(doc.coverage)}
                        text={percent(doc.coverage)}
                      />
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </Card>

      {/* `items-start` so a short card keeps its own height instead of being
          stretched to match the taller one beside it. */}
      <div className="grid items-start gap-6 lg:grid-cols-2">
        <Card>
          <SectionHeader
            title="Clauses found"
            subtitle="How many documents each clause type was located in."
            icon={ListChecks}
            action={
              <span className="text-xs tabular-nums text-slate-500 dark:text-slate-400">
                {clause_frequency.length} clause types
              </span>
            }
          />
          {clause_frequency.length === 0 ? (
            <EmptyState
              icon={SearchX}
              title="Nothing located yet"
              description="No clause has been matched to the taxonomy in any document."
            />
          ) : (
            // The chart needs 26px per bar to stay readable, so it is given that
            // height and the card scrolls rather than the bars being squashed.
            <div className="max-h-[28rem] overflow-y-auto">
              <ResponsiveContainer width="100%" height={Math.max(240, clause_frequency.length * 26)}>
                <BarChart
                  data={clause_frequency}
                  layout="vertical"
                  margin={{ left: 12, right: 16, top: 4, bottom: 4 }}
                >
                  <CartesianGrid strokeDasharray="3 3" horizontal={false} stroke="#e2e8f0" />
                  <XAxis type="number" allowDecimals={false} tick={{ fontSize: 12 }} />
                  <YAxis
                    type="category"
                    dataKey="clause"
                    width={190}
                    tick={{ fontSize: 11 }}
                    interval={0}
                  />
                  <Tooltip cursor={{ fill: '#f1f5f9' }} />
                  <Bar dataKey="documents" radius={[0, 4, 4, 0]}>
                    {clause_frequency.map((entry) => (
                      <Cell key={entry.clause} fill="#2563eb" />
                    ))}
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            </div>
          )}
        </Card>

        <Card>
          <SectionHeader
            title="Never located"
            subtitle="Expected by the taxonomy, not found in any document of that type."
            icon={SearchX}
            action={
              missing_clauses.length ? (
                <span className="text-xs tabular-nums text-slate-500 dark:text-slate-400">
                  {missing_clauses.length} gaps
                </span>
              ) : null
            }
          />
          {missing_clauses.length === 0 ? (
            <EmptyState
              icon={Sparkles}
              title="Full coverage"
              description="Every clause the taxonomy expects has been found at least once."
            />
          ) : (
            <>
              {/* Above the list, not below it: this caveat is how to read every
                  row, and at the bottom of a scrolling list it is the one thing
                  guaranteed to be off screen. */}
              <p className="mb-3 rounded-lg bg-slate-50 px-3 py-2 text-xs text-slate-500 dark:bg-slate-700/40 dark:text-slate-400">
                A clause listed here is either genuinely absent from these contracts or a
                detection gap. Both are worth knowing, and the two are not distinguishable
                from this screen alone.
              </p>
              <ul className="max-h-[24rem] divide-y divide-slate-100 overflow-y-auto dark:divide-slate-700">
                {missing_clauses.map((item) => (
                  <li key={`${item.doc_type}-${item.clause}`} className="flex items-center gap-2 py-3">
                    <Badge variant="warning" text={item.doc_type} />
                    <span className="min-w-0 truncate font-medium text-slate-900 dark:text-slate-100">
                      {item.clause}
                    </span>
                  </li>
                ))}
              </ul>
            </>
          )}
        </Card>
      </div>

      <Card>
        <SectionHeader
          title="Document types"
          subtitle="What has been processed, and the clause target for each type."
          icon={Layers}
        />
        <div className="flex flex-wrap gap-3">
          {by_doc_type.map((row) => (
            <div
              key={row.doc_type ?? 'unknown'}
              className="rounded-xl border border-slate-200 dark:border-slate-700 px-4 py-3"
            >
              <p className="text-sm font-medium text-slate-900 dark:text-slate-100">{row.doc_type ?? 'unknown'}</p>
              <p className="text-xs text-slate-500 dark:text-slate-400">
                {row.documents} document{row.documents === 1 ? '' : 's'} ·{' '}
                {row.clauses_expected} clauses expected
              </p>
            </div>
          ))}
        </div>
      </Card>
    </div>
  );
};

export default ClauseCoveragePage;
