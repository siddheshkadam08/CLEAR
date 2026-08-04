/**
 * Grouping for the processing queue.
 *
 * A ZIP upload becomes one contract and one job per document inside it, which is
 * the requirement - each has its own state, progress, retry and logs, and one
 * failing must not stop the others. The cost is that a fifty-file archive fills
 * the Jobs screen with fifty rows that look unrelated. Grouping is presentation
 * only: it never merges their fates.
 *
 * Lives outside the page component so it can be tested directly, and so the page
 * file keeps exporting only components (which is what React Fast Refresh needs).
 */

import type { JobListItem } from '@/api/types';

export interface JobGroup {
  /** Stable React key: the archive id, or the job's own id when ungrouped. */
  key: string;
  /** The archive's file name, or null for a directly uploaded document. */
  archive: string | null;
  jobs: JobListItem[];
}

/**
 * Collect *consecutive* jobs that came from the same archive.
 *
 * Consecutive rather than global, deliberately. The list is paginated and sorted
 * newest-first; regrouping across the whole page would reorder rows and quietly
 * break that guarantee, and on a page that happens to split an archive it would
 * pull rows from the far end of the list next to unrelated ones. Everything from
 * one archive is created by a single upload and so arrives adjacent anyway - this
 * preserves the server's order and only draws a box around a run already
 * together.
 *
 * A group of one is flattened: a ZIP that yielded a single document reads better
 * as an ordinary row than as a container holding one thing.
 */
export function groupByArchive(jobs: JobListItem[]): JobGroup[] {
  const groups: JobGroup[] = [];

  for (const job of jobs) {
    const archiveId = job.source_archive_id ?? null;
    const previous = groups[groups.length - 1];

    if (archiveId && previous && previous.key === archiveId) {
      previous.jobs.push(job);
      continue;
    }

    groups.push({
      key: archiveId ?? job.id,
      archive: archiveId ? (job.source_archive_name ?? 'Archive') : null,
      jobs: [job],
    });
  }

  return groups.map((group) =>
    group.archive !== null && group.jobs.length === 1 ? { ...group, archive: null } : group,
  );
}
