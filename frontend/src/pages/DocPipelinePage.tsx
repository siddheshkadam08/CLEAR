/**
 * Document pipeline insights.
 *
 * Reads only `cip_DocMaster` and `cip_DocContentMaster`, deliberately. The
 * overview dashboard counts clauses the older pipeline extracted into
 * `clauses`; this one counts clauses the document pipeline located and stored.
 * They are different definitions produced by different processes, and adding
 * them together would give a number that means nothing.
 *
 * Coverage is the figure to read. A raw clause count says little on its own -
 * eleven clauses is complete for an NDA and two-thirds of an MSA - so every
 * count is shown against what `cip_docMapping` says that document type should
 * have.
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

const percent = (value: number | null | undefined) =>
  value === null || value === undefined ? '--' : `${Math.round(value * 100)}%`;

/** Green at full coverage, amber past half, red below. */
const coverageVariant = (value: number | null | undefined) => {
  if (value === null || value === undefined) return 'neutral' as const;
  if (value >= 0.9) return 'success' as const;
  if (value >= 0.6) return 'warning' as const;
  return 'danger' as const;
};

export const DocPipelinePage = () => {
  const { data, isLoading, error } = useQuery({
    queryKey: ['docpipeline'],
    queryFn: () => api.insights(),
  });

  if (error) return <ErrorBanner message={errorMessage(error)} />;

  if (isLoading || !data) {
    return (
      <div className="space-y-5">
        <PageHeader
          title="Document pipeline"
          subtitle="Clauses located and stored by the document pipeline."
        />
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
        <PageHeader
          title="Document pipeline"
          subtitle="Clauses located and stored by the document pipeline."
        />
        <Card>
          <EmptyState
            icon={FileSearch}
            title="No documents processed yet"
            description="Run the pipeline over a folder of page JSON to populate these tables."
          />
        </Card>
      </div>
    );
  }

  return (
    <div className="space-y-5">
      <PageHeader
        title="Document pipeline"
        subtitle="Read from cip_DocMaster and cip_DocContentMaster only - separate from the contract pipeline's own counts."
      />

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
        />
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-slate-200 dark:border-slate-700 text-left text-xs uppercase tracking-wide text-slate-500 dark:text-slate-400">
                <th scope="col" className="pb-2 pr-4">Doc</th>
                <th scope="col" className="pb-2 pr-4">Type</th>
                <th scope="col" className="pb-2 pr-4">Clauses</th>
                <th scope="col" className="pb-2 pr-4">Coverage</th>
                <th scope="col" className="pb-2">Source</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {documents.map((doc) => (
                <tr key={doc.docid}>
                  <td className="py-3 pr-4 font-medium text-slate-900 dark:text-slate-100">#{doc.docid}</td>
                  <td className="py-3 pr-4">
                    <Badge variant="neutral" text={doc.doc_type ?? 'unknown'} />
                  </td>
                  <td className="py-3 pr-4 text-slate-700">
                    {doc.clauses_found} / {doc.clauses_expected}
                  </td>
                  <td className="py-3 pr-4">
                    <Badge
                      variant={coverageVariant(doc.coverage)}
                      text={percent(doc.coverage)}
                    />
                  </td>
                  <td
                    className="max-w-xs truncate py-3 text-xs text-slate-500"
                    title={doc.doc_path ?? doc.json_path ?? ''}
                  >
                    {doc.doc_path ?? doc.json_path ?? '--'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>

      <div className="grid gap-6 lg:grid-cols-2">
        <Card>
          <SectionHeader
            title="Clauses found"
            subtitle="How many documents each clause type was located in."
            icon={ListChecks}
          />
          {clause_frequency.length === 0 ? (
            <EmptyState icon={SearchX} title="Nothing located yet" description="" />
          ) : (
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
          )}
        </Card>

        <Card>
          <SectionHeader
            title="Never located"
            subtitle="Expected by the taxonomy, not found in any document of that type."
            icon={SearchX}
          />
          {missing_clauses.length === 0 ? (
            <EmptyState
              icon={Sparkles}
              title="Full coverage"
              description="Every clause the taxonomy expects has been found at least once."
            />
          ) : (
            <ul className="divide-y divide-slate-100">
              {missing_clauses.map((item) => (
                <li key={`${item.doc_type}-${item.clause}`} className="py-3">
                  <div className="flex items-center gap-2">
                    <Badge variant="warning" text={item.doc_type} />
                    <span className="font-medium text-slate-900 dark:text-slate-100">{item.clause}</span>
                  </div>
                  {item.description ? (
                    <p className="mt-1 text-xs text-slate-500">{item.description}</p>
                  ) : null}
                </li>
              ))}
            </ul>
          )}
          <p className="mt-4 text-xs text-slate-400">
            A clause listed here is either genuinely absent from these contracts or a
            detection gap. Both are worth knowing, and the two are not distinguishable
            from this screen alone.
          </p>
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
              <p className="text-xs text-slate-500">
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

export default DocPipelinePage;
