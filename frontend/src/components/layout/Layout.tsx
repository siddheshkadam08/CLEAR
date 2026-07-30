/**
 * App shell: sidebar + sticky header + main.
 *
 * The page title is resolved here from a route map by longest-prefix match, never
 * hardcoded inside a page. A title that lives in two places drifts in one of them.
 *
 * Z-index ladder: header 20 · mobile scrim 30 · sidebar 40 · modals 50.
 */

import { Menu } from 'lucide-react';
import { useState } from 'react';
import type { ReactNode } from 'react';
import { useLocation } from 'react-router-dom';

import { useAuth } from '@/lib/auth';
import { APP_NAME, initialsOf } from '@/lib/identity';
import { useProjectScope } from '@/lib/scope';
import { Sidebar } from './Sidebar';

const TITLES: Record<string, string> = {
  '/': 'Dashboard',
  '/upload': 'Upload contracts',
  '/contracts': 'Contracts',
  '/search': 'Search',
  '/copilot': 'Copilot',
  '/jobs': 'Processing',
  '/alerts': 'Alerts',
  '/clause-master': 'Clause Master',
  '/admin/projects': 'Projects',
  '/admin/users': 'Users',
};

const titleFor = (pathname: string) => {
  if (pathname === '/') return TITLES['/'];
  const match = Object.keys(TITLES)
    .filter((key) => key !== '/' && pathname.startsWith(key))
    .sort((a, b) => b.length - a.length)[0];
  return match ? TITLES[match] : APP_NAME;
};

export const Layout = ({ children }: { children: ReactNode }) => {
  const [isSidebarOpen, setIsSidebarOpen] = useState(false);
  const { user } = useAuth();
  const { projects, projectId, setProjectId } = useProjectScope();
  const { pathname } = useLocation();

  return (
    <div className="min-h-screen bg-slate-50">
      <Sidebar isOpen={isSidebarOpen} onClose={() => setIsSidebarOpen(false)} />

      <div className="lg:pl-64">
        <header className="sticky top-0 z-20 border-b border-slate-200 bg-white/90 backdrop-blur">
          <div className="flex items-center justify-between gap-3 px-4 py-3 sm:gap-4 sm:px-6 sm:py-4 lg:px-8">
            <div className="flex min-w-0 items-center gap-3">
              <button
                type="button"
                onClick={() => setIsSidebarOpen(true)}
                title="Open navigation"
                aria-label="Open navigation"
                className="shrink-0 rounded-xl border border-slate-200 p-2 text-slate-600 shadow-sm transition hover:bg-slate-50 lg:hidden"
              >
                <Menu className="h-5 w-5" />
              </button>
              <div className="min-w-0">
                <p className="hidden text-xs font-semibold uppercase tracking-[0.2em] text-slate-400 sm:block">
                  Workspace
                </p>
                <h2 className="truncate text-lg font-semibold text-slate-900 sm:text-2xl">
                  {titleFor(pathname)}
                </h2>
              </div>
            </div>

            <div className="flex shrink-0 items-center gap-2 sm:gap-3">
              {/* Project selector. "All my projects" is literal: the server resolves
                  scope from membership, so this narrows that set and can never
                  widen it. Hidden below `md`, where the sidebar's own scope control
                  and the narrower pages carry it instead. */}
              <label className="hidden items-center gap-2 md:flex">
                <span className="text-xs font-semibold uppercase tracking-wider text-slate-400">
                  Project
                </span>
                <select
                  value={projectId ?? ''}
                  onChange={(event) => setProjectId(event.target.value || null)}
                  className="w-40 rounded-xl border border-slate-200 px-3 py-2 text-sm text-slate-900 outline-none transition focus:border-blue-500 focus:ring-2 focus:ring-blue-100 lg:w-48"
                >
                  <option value="">All my projects</option>
                  {projects.map((project) => (
                    <option key={project.id} value={project.id}>
                      {project.name}
                    </option>
                  ))}
                </select>
              </label>

              <div className="flex items-center gap-3 rounded-full border border-slate-200 bg-white px-2 py-1.5 shadow-sm sm:px-3 sm:py-2">
                <div className="hidden text-right lg:block">
                  <p className="max-w-[12rem] truncate text-sm font-semibold text-slate-900">
                    {user?.full_name ?? 'User'}
                  </p>
                  <p className="text-xs text-slate-500">
                    {user?.is_system_admin ? 'Administrator' : 'Project member'}
                  </p>
                </div>
                <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-blue-600 text-xs font-semibold text-white sm:h-10 sm:w-10 sm:text-sm">
                  {initialsOf(user?.full_name) || 'XX'}
                </div>
              </div>
            </div>
          </div>

          {/* Below `md` the project scope moves under the title so it is still
              reachable without a horizontally cramped header row. */}
          <div className="border-t border-slate-100 px-4 py-2 md:hidden">
            <select
              value={projectId ?? ''}
              onChange={(event) => setProjectId(event.target.value || null)}
              aria-label="Project scope"
              className="w-full rounded-xl border border-slate-200 px-3 py-2 text-sm text-slate-900 outline-none transition focus:border-blue-500 focus:ring-2 focus:ring-blue-100"
            >
              <option value="">All my projects</option>
              {projects.map((project) => (
                <option key={project.id} value={project.id}>
                  {project.name}
                </option>
              ))}
            </select>
          </div>
        </header>

        <main className="px-4 py-4 sm:px-6 sm:py-6 lg:px-8">{children}</main>
      </div>
    </div>
  );
};

export default Layout;
