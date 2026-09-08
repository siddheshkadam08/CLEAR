/**
 * The routes and their names must agree, and nothing else checks that.
 *
 * Five live routes - /portfolio, /search, /exports, /admin/evaluation and
 * /admin/audit - shipped with no entry in the title map, so the top bar showed
 * the product name instead of the screen's. Every one of them typechecked: the
 * map was a `Record<string, string>` and a missing key is simply a missing key.
 * Only the *pair* of files was wrong, and only at runtime, on screens nobody
 * re-opened.
 *
 * So this reads `App.tsx` as text rather than importing it. The routes are JSX
 * inside a component wrapped in three guards and fifteen `lazy()` calls;
 * rendering them would need a Router, a QueryClient and a signed-in user, and
 * would pull in every chunk. The characters are the thing under test anyway.
 */

import { describe, expect, it } from 'vitest';

import { APP_NAME } from '@/lib/identity';
// `?raw` gives the file's text through the same resolver the app uses, so this
// cannot drift from the module graph or depend on the working directory.
import APP_SOURCE from '../../App.tsx?raw';
import { LEGACY_PATHS, NAV_ROUTES, ROUTES, titleFor } from './navigation';

/**
 * Every path App declares. `<Route index>` carries no `path` attribute, so it is
 * added by hand - guarded, so deleting the index route fails here rather than
 * silently dropping "/" from the set this file checks.
 */
const declaredPaths = [
  ...(/<Route\s+index\b/.test(APP_SOURCE) ? ['/'] : []),
  ...[...APP_SOURCE.matchAll(/\bpath="([^"]+)"/g)].map((match) => {
    const path = match[1] ?? '';
    return path.startsWith('/') ? path : `/${path}`;
  }),
];

/**
 * Routes rendered outside `AppShell`, which have no top bar to title. Listed
 * rather than inferred: a new route defaults to needing a name, and opting one
 * out is a line somebody has to write.
 */
const OUTSIDE_SHELL = [
  '/login',
  '/auth/callback',
  '/change-password',
  // Reached from a password-reset email by someone with no session, so they sit
  // outside `ProtectedRoute` and have no shell to title.
  '/forgot-password',
  '/reset-password',
  '/*',
];

/** `/contracts/:contractId` resolves by prefix, so any id will do. */
const withSampleParams = (path: string) => path.replace(/:[^/]+/g, 'sample-id');

describe('every route has a name', () => {
  it('is actually reading the route table', () => {
    // Guards the regex, not the routes: if the JSX were reformatted so that
    // `path="..."` no longer appeared literally, every assertion below would
    // pass vacuously.
    expect(declaredPaths.length).toBeGreaterThan(15);
    expect(declaredPaths).toContain('/contracts/:contractId');
  });

  it('excludes only routes that still exist', () => {
    for (const path of OUTSIDE_SHELL) {
      expect(declaredPaths, `${path} is no longer declared`).toContain(path);
    }
  });

  it('titles every route rendered inside the shell', () => {
    for (const path of declaredPaths) {
      if (OUTSIDE_SHELL.includes(path)) continue;
      // A redirect has no title of its own; its destination must have one.
      const resolved = LEGACY_PATHS[path] ?? path;
      expect(
        titleFor(withSampleParams(resolved)),
        `${path} falls back to the product name in the top bar`,
      ).not.toBe(APP_NAME);
    }
  });

  it('names no route that no longer exists', () => {
    // The other direction: a title left behind by a deleted or renamed route is
    // how `/doc-pipeline` outlived itself in two files.
    for (const route of ROUTES) {
      expect(declaredPaths, `${route.path} is named but not routed`).toContain(route.path);
    }
  });

  it('offers no sidebar link to a route that does not exist', () => {
    for (const route of NAV_ROUTES) {
      expect(declaredPaths, `${route.path} is in the sidebar`).toContain(route.path);
    }
  });

  it('names each screen once', () => {
    const paths = ROUTES.map((route) => route.path);
    expect(new Set(paths).size).toBe(paths.length);
    const titles = ROUTES.map((route) => route.title);
    expect(new Set(titles).size).toBe(titles.length);
  });

  it('keeps every legacy path pointing somewhere real', () => {
    for (const [from, to] of Object.entries(LEGACY_PATHS)) {
      expect(declaredPaths, `${from} redirects but is not routed`).toContain(from);
      expect(declaredPaths, `${from} redirects to ${to}, which is not routed`).toContain(to);
    }
  });
});

describe('titleFor', () => {
  it('titles a detail route from its parent', () => {
    expect(titleFor('/contracts/9f1c-abc')).toBe('Contracts');
  });

  it('matches whole segments, not raw string prefixes', () => {
    // The old implementation used `startsWith`, which would have titled this
    // "Contracts" - wrong, and invisible until such a route exists.
    expect(titleFor('/contracts-archive')).toBe(APP_NAME);
  });

  it('resolves the root exactly', () => {
    expect(titleFor('/')).toBe('Dashboard');
  });
});
