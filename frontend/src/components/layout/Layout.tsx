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
        <header className="sticky top-0 z-20 border-b border-[#E4E7EC] bg-white">
          <div className="flex h-[60px] items-center justify-between gap-3 px-4 sm:gap-4 sm:px-6 lg:px-8">
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
                <p className="hidden font-mono text-[10px] font-semibold uppercase tracking-[1.2px] text-[#94A0B4] sm:block">
                  Workspace
                </p>
                <h2 className="truncate text-[18px] font-semibold text-[#0F172A] sm:text-[20px]">
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
                <span className="font-mono text-[10px] font-semibold uppercase tracking-[1.2px] text-[#94A0B4]">
                  Business Unit
                </span>
                <div className="relative">
                  <select
                    value={projectId ?? ''}
                    onChange={(event) => setProjectId(event.target.value || null)}
                    className="h-9 w-40 cursor-pointer appearance-none rounded-lg border border-[#E4E7EC] bg-white py-0 pl-3 pr-8 text-[13px] font-medium text-[#0F172A] outline-none transition hover:border-[#94A0B4] focus:border-[#2563EB] focus:ring-2 focus:ring-blue-100 lg:w-48"
                  >
                    <option value="">All Business Units</option>
                    {projects.map((project) => (
                      <option key={project.id} value={project.id}>
                        {project.name}
                      </option>
                    ))}
                  </select>
                  <svg className="pointer-events-none absolute right-2.5 top-1/2 -translate-y-1/2 text-[#5B6478]" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><path d="M6 9l6 6 6-6" /></svg>
                </div>
              </label>

              <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-[#2563EB] text-xs font-semibold text-white sm:h-9 sm:w-9">
                {initialsOf(user?.full_name) || 'XX'}
              </div>
            </div>
          </div>

          {/* Below `md` the project scope moves under the title so it is still
              reachable without a horizontally cramped header row. */}
          <div className="border-t border-[#E4E7EC] px-4 py-2 md:hidden">
            <div className="relative">
              <select
                value={projectId ?? ''}
                onChange={(event) => setProjectId(event.target.value || null)}
                aria-label="Project scope"
                className="h-9 w-full cursor-pointer appearance-none rounded-lg border border-[#E4E7EC] bg-white py-0 pl-3 pr-8 text-[13px] font-medium text-[#0F172A] outline-none transition hover:border-[#94A0B4] focus:border-[#2563EB] focus:ring-2 focus:ring-blue-100"
              >
                <option value="">All my business units</option>
                {projects.map((project) => (
                  <option key={project.id} value={project.id}>
                    {project.name}
                  </option>
                ))}
              </select>
              <svg className="pointer-events-none absolute right-2.5 top-1/2 -translate-y-1/2 text-[#5B6478]" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><path d="M6 9l6 6 6-6" /></svg>
            </div>
          </div>
        </header>

        <main className="px-4 py-4 sm:px-6 sm:py-6 lg:px-8">{children}</main>
      </div>
    </div>
  );
};

export default Layout;
