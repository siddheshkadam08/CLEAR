/**
 * Guards two agreements with the backend that nothing else checks.
 *
 * Both bugs these cover typechecked perfectly and failed only at runtime, because
 * a TypeScript type describes what the frontend *writes*, never what the server
 * *sends* - and FastAPI drops an unknown query parameter without complaint. So
 * the compiler cannot see either mistake, and neither test suite could either:
 * each side was self-consistent, and only the pair was wrong.
 */

import { describe, expect, it } from 'vitest';

import { contracts } from './endpoints';
import type { JobState } from './types';

describe('JobState casing', () => {
  it('is upper case, matching the backend StrEnum', () => {
    // `JobState` on the backend is the one enum whose values are upper case.
    // This type said 'ready'; the API sends 'READY'. Every comparison against a
    // lower-case literal was quietly false - the Jobs page polled every 4s for
    // ever because no job ever looked terminal, and the state filter 422'd.
    const states: JobState[] = ['QUEUED', 'READY', 'FAILED', 'CANCELLED'];
    for (const state of states) {
      expect(state).toBe(state.toUpperCase());
    }

    // @ts-expect-error lower case is no longer assignable - this is the guard.
    const wrong: JobState = 'ready';
    expect(wrong).toBeDefined();
  });
});

describe('contract list free-text parameter', () => {
  it('sends `search`, the name the endpoint declares', async () => {
    // It sent `q`. FastAPI ignores parameters it does not declare, so the search
    // box returned the full unfiltered list and looked like a term that matched
    // everything. Measured against the running API: `q=sponsorship` returned all
    // 9 contracts, `search=sponsorship` returned 1.
    const seen: string[] = [];
    const originalFetch = globalThis.fetch;
    globalThis.fetch = ((input: RequestInfo | URL) => {
      seen.push(String(input));
      return Promise.resolve(
        new Response('{"items":[],"total":0,"page":1,"size":20}', {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      );
    }) as typeof fetch;

    try {
      await contracts.list(null, { search: 'sponsorship' });
    } finally {
      globalThis.fetch = originalFetch;
    }

    expect(seen[0]).toContain('search=sponsorship');
    expect(seen[0]).not.toContain('q=sponsorship');
  });
});
