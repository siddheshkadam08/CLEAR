/**
 * A screen that reads project-scoped data must key its cache on the business unit.
 *
 * The selector re-scopes the application by changing the react-query key: that is
 * the whole mechanism, and there is no global cache reset behind it. So a page that
 * fetches scoped rows under a key without `projectId` keeps showing the previous
 * unit's data after a switch - and shows it as though it were current, which is the
 * part that makes it dangerous rather than merely wrong.
 *
 * Four screens had this at once: Clause Coverage did not send the project at all,
 * Exports and Copilot conversations listed every unit, and Upload targeted whichever
 * unit was selected when the page mounted - so a file could be uploaded into the
 * wrong one. All four were correct-looking code that typechecked.
 *
 * Reading the sources as text is deliberate. The alternative - rendering every page
 * and asserting on refetches - needs a router, a query client and a signed-in user
 * per page, and would still only cover the ones somebody remembered to add.
 */

import { describe, expect, it } from 'vitest';

const PAGES = import.meta.glob('../pages/**/*.tsx', { eager: true, query: '?raw', import: 'default' });
const COMPONENTS = import.meta.glob('../components/**/*.tsx', { eager: true, query: '?raw', import: 'default' });
// `.ts` too, for the staleness check below - `['projects']` is issued from
// `lib/scope.ts`, which has no JSX and would otherwise look like a dead exemption.
const LIB = import.meta.glob('../lib/**/*.ts', { eager: true, query: '?raw', import: 'default' });

const SOURCES: Record<string, string> = Object.fromEntries(
  Object.entries({ ...PAGES, ...COMPONENTS, ...LIB })
    .filter(([path]) => !/\.test\.tsx?$/.test(path))
    .map(([path, source]) => [path.replace('../', ''), source as string]),
);

/**
 * Keys that legitimately carry no business unit, each with the reason.
 *
 * Adding to this is how you disagree with the rule - in writing, where the next
 * person can see the argument rather than guess whether it was a decision.
 */
const UNSCOPED_KEYS: Record<string, string> = {
  'pipeline-health': 'platform-wide worker and queue health; has no project dimension',
  job: 'a single job by id - the id already fixes the project',
  contract: 'a single contract by id',
  'contract-file': 'one contract, by id',
  'contract-jobs': 'the jobs of one contract, by id',
  'contract-graph': 'the graph of one contract, by id',
  knowledge: 'the extracted knowledge of one contract, by id',
  'export-capabilities': 'static configuration - which formats exist',
  export: 'a single export by id',
  'clause-master': 'the clause taxonomy is global, not per business unit',
  'auth-methods': 'which sign-in methods exist, before anyone is signed in',
  projects: 'the list of business units itself - scoping it would empty the selector',
  admin: 'administration screens are platform-wide by definition',
  'evaluation-runs': 'benchmark runs are platform-wide',
  'evaluation-latest': 'benchmark runs are platform-wide',
};

/**
 * Every `queryKey` a file *reads* under, as literal source text.
 *
 * Invalidations are excluded on purpose. `invalidateQueries({queryKey: ['jobs']})`
 * is a prefix match, so it already invalidates every `['jobs', projectId, ...]`
 * beneath it - adding the project there would *narrow* the invalidation and leave
 * the other units' caches stale, which is the opposite of what is wanted.
 */
function queryKeysIn(source: string): string[] {
  const keys: string[] = [];
  for (const match of source.matchAll(/queryKey:\s*\[([^\]]*)\]/g)) {
    // Look *backwards* from the match rather than trying to express "not preceded
    // by" in the pattern: a lazy prefix group simply lets the engine start the
    // match after the word it was meant to exclude.
    const preceding = source.slice(Math.max(0, (match.index ?? 0) - 60), match.index);
    if (/invalidateQueries|removeQueries|cancelQueries|setQueryData/.test(preceding)) continue;
    keys.push(match[1] ?? '');
  }
  return keys;
}

/** The first entry, which is the name the exemption list is keyed on. */
function keyNameOf(key: string): string {
  return (key.split(',')[0] ?? '').trim().replace(/^['"`]|['"`]$/g, '');
}

const SCOPED_FILES = Object.entries(SOURCES).filter(([, source]) =>
  source.includes('useProjectScope'),
);

describe('business-unit scoping', () => {
  it('is actually reading the sources', () => {
    // Guards the glob: if it matched nothing, every assertion below would pass
    // while checking no files at all.
    expect(Object.keys(SOURCES).length).toBeGreaterThan(15);
    expect(SCOPED_FILES.length).toBeGreaterThan(5);
  });

  it.each(SCOPED_FILES.map(([path]) => path))(
    '%s keys every cached read on the business unit',
    (path) => {
      const source = SOURCES[path] ?? '';
      for (const key of queryKeysIn(source)) {
        const name = keyNameOf(key);
        if (name in UNSCOPED_KEYS) continue;
        expect(
          key.includes('projectId'),
          `queryKey [${key.trim()}] in ${path} does not include projectId, so it will ` +
            'keep serving the previous business unit after the header changes. Add it, ' +
            'or name the key in UNSCOPED_KEYS with why it has no project dimension.',
        ).toBe(true);
      }
    },
  );

  it('exempts no key that has stopped existing', () => {
    // A stale exemption silently excuses a key nobody is using, and would excuse a
    // new one that happened to reuse the name.
    // Every key name in the app, invalidations included: this asks only whether
    // the name is still in use anywhere, not whether that use needs scoping.
    const used = new Set(
      Object.values(SOURCES).flatMap((source) =>
        [...source.matchAll(/queryKey:\s*\[([^\]]*)\]/g)].map((match) =>
          keyNameOf(match[1] ?? ''),
        ),
      ),
    );
    for (const name of Object.keys(UNSCOPED_KEYS)) {
      expect(used, `UNSCOPED_KEYS exempts '${name}', which is no longer a query key`).toContain(
        name,
      );
    }
  });

  it('sends the project to the two endpoints that only recently accepted one', () => {
    // Both were scoped to the requester and to nothing else, so they listed every
    // business unit. The server filter is additive - owner-only is unchanged.
    expect(SOURCES['pages/ExportsPage.tsx']).toContain('projectId');
    expect(SOURCES['pages/CopilotPage.tsx']).toContain('copilotApi.sessions(projectId)');
  });
});
