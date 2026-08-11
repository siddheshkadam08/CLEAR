/**
 * Where every screen's name lives.
 *
 * A screen appears in three places - the sidebar link, the title in the top bar,
 * and its own body - and until this module existed it was named separately in
 * each. All three had drifted: the same screen was "Doc Pipeline" in the nav and
 * "Document pipeline" in the top bar, four screens printed their name twice, and
 * five live routes were named nowhere at all, so the shell fell back to the
 * product name and the top bar read "C.L.E.A.R." on Portfolio, Search, Exports,
 * Retrieval Quality and Activity.
 *
 * So: one record per route, one `title`. The body no longer names itself at all
 * (see `PageHeader`), and the sidebar reuses the title unless `nav.label` says
 * otherwise - which is a decision to argue for, not a default.
 *
 * `nav` absent means reachable but not offered - a route that needs a name without
 * needing a link. The old nav-array-plus-title-map could not express that at all,
 * which is how `/copilot` ended up the one route that *had* a title while five
 * routes with links had none. Retrieval Quality uses it today.
 */

import {
  AlertTriangle,
  Bot,
  FileDown,
  FileSearch,
  FileText,
  FolderKanban,
  Landmark,
  LayoutDashboard,
  ListChecks,
  ScrollText,
  Search,
  Settings2,
  Upload,
  Users,
} from 'lucide-react';
import type { LucideIcon } from 'lucide-react';

import { APP_NAME } from '@/lib/identity';

/** Who a nav item is for. `member` means "everyone except the administrator". */
export type Audience = 'all' | 'admin' | 'member';

/** Present only on routes the sidebar offers. */
export interface NavPlacement {
  icon: LucideIcon;
  /**
   * Sidebar text, when it must differ from `title`.
   *
   * Only `/upload` uses it: the top bar has room for "Upload contracts" and the
   * 256px rail does not. Every use is a second name for one screen, so each one
   * needs a reason written next to it.
   */
  label?: string;
  audience?: Audience;
  /**
   * Show only to a holder of this permission, on any project.
   *
   * `audience` cannot express this: some screens are open to a role rather than
   * to administrators, and Activity is the first - AUDIT_READ belongs to Project
   * Manager, so `audience: 'admin'` would hide it from the people it is for.
   */
  permission?: string;
  section?: string;
}

export interface RouteMeta {
  /** Path as the router declares it, leading slash included. */
  path: string;
  /** The screen's name. Top bar always; sidebar unless `nav.label` overrides. */
  title: string;
  nav?: NavPlacement;
}

/** A route the sidebar offers - `nav` narrowed to present. */
export type NavRoute = RouteMeta & { nav: NavPlacement };

/**
 * Ordered by workflow rather than alphabetically: upload a contract, review what
 * came out, ask questions across the repository, then watch the machinery and
 * configure it. That order is the product's own story, and the sidebar renders
 * this array in order.
 *
 * Two audiences see two different lists. An administrator governs the platform -
 * business units, people, master data - and reads everything, but does not put
 * contracts into it; a project member does the contract work. `Upload` is
 * therefore absent for an administrator rather than present-and-rejected, which
 * would advertise a screen whose every submission returns 403.
 */
export const ROUTES: RouteMeta[] = [
  { path: '/', title: 'Dashboard', nav: { icon: LayoutDashboard } },
  {
    path: '/upload',
    title: 'Upload contracts',
    nav: { icon: Upload, label: 'Upload', audience: 'member' },
  },
  { path: '/contracts', title: 'Contracts', nav: { icon: FileText } },
  // The cross-contract registers. Sits next to Contracts because it is the same
  // corpus read the other way round: by obligation, date, risk and counterparty
  // rather than by document.
  { path: '/portfolio', title: 'Portfolio', nav: { icon: Landmark } },
  { path: '/search', title: 'Search', nav: { icon: Search } },
  // Next to Search because both ask the repository a question - Search returns the
  // passages, Copilot returns the answer drawn from them.
  //
  // There is deliberately a second way in, from the drawer on a contract, and the
  // two are not the same feature wearing two hats: the drawer answers about *this
  // agreement* and is hard-scoped to it, while this page answers across every
  // contract in the business unit. "What does this contract say about indemnities"
  // and "which of our contracts have uncapped indemnities" are different questions,
  // and only one of them has a contract to open it from.
  { path: '/copilot', title: 'Copilot', nav: { icon: Bot } },
  { path: '/jobs', title: 'Processing', nav: { icon: ListChecks } },
  // Was "Doc Pipeline" / "Document pipeline". Renamed for what it measures: how
  // much of each document type's expected clause set was actually found.
  { path: '/clause-coverage', title: 'Clause Coverage', nav: { icon: FileSearch } },
  { path: '/alerts', title: 'Alerts', nav: { icon: AlertTriangle } },
  { path: '/exports', title: 'Exports', nav: { icon: FileDown } },
  {
    path: '/admin/projects',
    title: 'Business Unit',
    nav: { icon: FolderKanban, audience: 'admin', section: 'Administration' },
  },
  {
    path: '/admin/users',
    title: 'Users',
    nav: { icon: Users, audience: 'admin', section: 'Administration' },
  },
  {
    path: '/clause-master',
    title: 'Clause Master',
    nav: { icon: Settings2, audience: 'admin', section: 'Administration' },
  },
  // Hidden from the sidebar for everyone, administrators included. The screen
  // reads benchmark runs that only exist once somebody runs the CLI, so for the
  // people who have not it is a permanent empty state - a nav entry that only
  // ever leads to "no benchmark has been recorded" teaches people to ignore the
  // menu. Still routed, still admin-gated: go to /admin/evaluation directly.
  { path: '/admin/evaluation', title: 'Retrieval Quality' },
  {
    path: '/admin/audit',
    title: 'Activity',
    nav: { icon: ScrollText, permission: 'audit:read', section: 'Administration' },
  },
];

/**
 * Paths that moved. A bookmark is a promise, so the old path still resolves - and
 * the route test reads this map, so a redirect cannot be added without the
 * destination having a name.
 */
export const LEGACY_PATHS: Record<string, string> = {
  '/doc-pipeline': '/clause-coverage',
};

export const NAV_ROUTES: NavRoute[] = ROUTES.filter(
  (route): route is NavRoute => route.nav !== undefined,
);

export const visibleTo = (
  nav: NavPlacement,
  isAdmin: boolean,
  permissions: Set<string>,
): boolean => {
  if (nav.permission) return isAdmin || permissions.has(nav.permission);
  if (nav.audience === 'admin') return isAdmin;
  if (nav.audience === 'member') return !isAdmin;
  return true;
};

/** Sidebar text for a route: the title, unless the rail needs a shorter one. */
export const navLabelOf = (route: NavRoute): string => route.nav.label ?? route.title;

/**
 * Longest matching prefix, so `/contracts/<id>` is titled "Contracts" - the shell
 * cannot know a contract's name, and that page prints it itself.
 *
 * The match is on whole segments (`=== path` or `startsWith(path + '/')`), not on
 * raw `startsWith` as the old `titleFor` did: raw prefix matching would title a
 * hypothetical `/contracts-archive` "Contracts", which is the kind of wrong that
 * is invisible until the route exists.
 */
export const titleFor = (pathname: string): string => {
  if (pathname === '/') {
    return ROUTES.find((route) => route.path === '/')?.title ?? APP_NAME;
  }
  let best: RouteMeta | undefined;
  for (const route of ROUTES) {
    if (route.path === '/') continue;
    const hit = pathname === route.path || pathname.startsWith(`${route.path}/`);
    if (hit && (!best || route.path.length > best.path.length)) best = route;
  }
  return best?.title ?? APP_NAME;
};
