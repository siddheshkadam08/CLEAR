/**
 * Grouping must never change what the list says about any individual job.
 *
 * A ZIP produces one independent job per document - own state, progress, retry,
 * logs - and that independence is the requirement. Grouping is a box drawn
 * around rows that arrived together; these pin the line between the two.
 */

import { describe, expect, it } from 'vitest';

import type { JobListItem } from '@/api/types';

import { groupByArchive } from './job-grouping';

function job(id: string, archive?: { id: string; name: string }): JobListItem {
  return {
    id,
    contract_id: `c-${id}`,
    project_id: 'p1',
    state: 'QUEUED',
    progress: 0,
    retry_count: 0,
    created_at: '2026-08-03T00:00:00Z',
    source_archive_id: archive?.id ?? null,
    source_archive_name: archive?.name ?? null,
  } as JobListItem;
}

const ZIP = { id: 'zip-1', name: 'Contracts.zip' };
const OTHER = { id: 'zip-2', name: 'More.zip' };

describe('groupByArchive', () => {
  it('leaves directly uploaded documents ungrouped', () => {
    const groups = groupByArchive([job('a'), job('b')]);

    expect(groups).toHaveLength(2);
    expect(groups.every((group) => group.archive === null)).toBe(true);
  });

  it('collects documents from one archive under its name', () => {
    const groups = groupByArchive([job('a', ZIP), job('b', ZIP), job('c', ZIP)]);

    expect(groups).toHaveLength(1);
    expect(groups[0]?.archive).toBe('Contracts.zip');
    expect(groups[0]?.jobs.map((entry) => entry.id)).toEqual(['a', 'b', 'c']);
  });

  it('keeps two archives apart', () => {
    // Two jobs each: a one-document archive is flattened by design, which would
    // make this pass for the wrong reason.
    const groups = groupByArchive([
      job('a', ZIP),
      job('b', ZIP),
      job('c', OTHER),
      job('d', OTHER),
    ]);

    expect(groups.map((group) => group.archive)).toEqual(['Contracts.zip', 'More.zip']);
  });

  it('never reorders the list', () => {
    // The queue is sorted newest-first and paginated. Regrouping globally would
    // pull a straggler up next to its siblings and silently break that order.
    const input = [job('a', ZIP), job('loose'), job('b', ZIP)];

    const flattened = groupByArchive(input).flatMap((group) => group.jobs);

    expect(flattened.map((entry) => entry.id)).toEqual(['a', 'loose', 'b']);
  });

  it('does not merge an archive split by an unrelated row', () => {
    const groups = groupByArchive([job('a', ZIP), job('loose'), job('b', ZIP)]);

    expect(groups).toHaveLength(3);
    // `a` and `b` are each alone, so each is flattened back to an ordinary row.
    expect(groups.map((group) => group.archive)).toEqual([null, null, null]);
  });

  it('flattens an archive that yielded a single document', () => {
    // A container holding one thing is worse than no container.
    const groups = groupByArchive([job('only', ZIP)]);

    expect(groups).toHaveLength(1);
    expect(groups[0]?.archive).toBeNull();
    expect(groups[0]?.jobs).toHaveLength(1);
  });

  it('every job survives exactly once', () => {
    const input = [job('a', ZIP), job('b', ZIP), job('c'), job('d', OTHER), job('e', OTHER)];

    const flattened = groupByArchive(input).flatMap((group) => group.jobs);

    expect(flattened).toHaveLength(input.length);
    expect(new Set(flattened.map((entry) => entry.id)).size).toBe(input.length);
  });

  it('falls back to a label when the archive name is missing', () => {
    const nameless = { ...job('a'), source_archive_id: 'zip-x', source_archive_name: null };

    const groups = groupByArchive([nameless as JobListItem, { ...nameless, id: 'b' } as JobListItem]);

    expect(groups[0]?.archive).toBe('Archive');
  });

  it('handles an empty list', () => {
    expect(groupByArchive([])).toEqual([]);
  });

  it('gives every group a distinct key', () => {
    const groups = groupByArchive([job('a', ZIP), job('b'), job('c', OTHER), job('d', OTHER)]);

    expect(new Set(groups.map((group) => group.key)).size).toBe(groups.length);
  });
});
