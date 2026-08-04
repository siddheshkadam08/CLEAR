/**
 * App shell: sidebar + sticky header + main.
 *
 * The page title is resolved here from a route map by longest-prefix match, never
 * hardcoded inside a page. A title that lives in two places drifts in one of them.
 *
 * Z-index ladder: header 20 · mobile scrim 30 · sidebar 40 · modals 50.
 */

import { Bell, Menu, Moon, Sun } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { useLocation } from 'react-router-dom';

import { useAuth } from '@/lib/auth';
import { APP_NAME, initialsOf } from '@/lib/identity';
import { useProjectScope } from '@/lib/scope';
import { useTheme } from '@/lib/theme';
import { Sidebar } from './Sidebar';

const TITLES: Record<string, string> = {
  '/': 'Dashboard',
  '/upload': 'Upload contracts',
  '/contracts': 'Contracts',
  // '/search': 'Search',
  '/copilot': 'Copilot',
  '/jobs': 'Processing',
  '/doc-pipeline': 'Document pipeline',
  '/alerts': 'Alerts',
  '/clause-master': 'Clause Master',
  '/admin/projects': 'Business Unit',
  '/admin/users': 'Users',
};

const titleFor = (pathname: string) => {
  if (pathname === '/') return TITLES['/'];
  const match = Object.keys(TITLES)
    .filter((key) => key !== '/' && pathname.startsWith(key))
    .sort((a, b) => b.length - a.length)[0];
  return match ? TITLES[match] : APP_NAME;
};

const NOTIFICATIONS = [
  {
    id: 1,
    title: 'Contract expiring soon',
    message: 'Vendor Agreement with Acme Corp expires in 7 days.',
    time: '2 min ago',
    read: false,
  },
  {
    id: 2,
    title: 'Processing complete',
    message: 'NDA_2026_Final.pdf has finished extraction.',
    time: '18 min ago',
    read: false,
  },
  {
    id: 3,
    title: 'High-risk clause detected',
    message: 'Unlimited liability clause found in MSA_Globex.pdf.',
    time: '1 hr ago',
    read: true,
  },
  {
    id: 4,
    title: 'Member added',
    message: 'Sarah Johnson was added to the Procurement project.',
    time: '3 hr ago',
    read: true,
  },
  {
    id: 5,
    title: 'Contract uploaded',
    message: 'SLA_TechPartners_2026.pdf is now processing.',
    time: 'Yesterday',
    read: true,
  },
];

/** Custom dropdown that caps the list at 6 visible options with a scrollbar. */
const ProjectDropdown = ({
  value,
  onChange,
  projects,
  placeholder = 'All Business Units',
  className = '',
}: {
  value: string;
  onChange: (val: string | null) => void;
  projects: { id: string; name: string }[];
  placeholder?: string;
  className?: string;
}) => {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  // Held separately rather than read back as `options[0]`: the compiler cannot
  // see that a spread-built array is non-empty, so indexing it is `T | undefined`.
  const fallback = { id: '', name: placeholder };
  const options = [fallback, ...projects];
  const selected = options.find((o) => o.id === value) ?? fallback;

  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, []);

  return (
    <div ref={ref} className={`relative ${className}`}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex h-9 w-full cursor-pointer items-center rounded-lg border border-[#E4E7EC] bg-white py-0 pl-3 pr-8 text-left text-[13px] font-medium text-[#0F172A] outline-none transition hover:border-[#94A0B4] dark:border-slate-600 dark:bg-slate-800 dark:text-slate-100 dark:hover:border-slate-500"
      >
        <span className="block truncate">{selected.name}</span>
      </button>
      <svg className="pointer-events-none absolute right-2.5 top-1/2 -translate-y-1/2 text-[#5B6478]" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><path d="M6 9l6 6 6-6" /></svg>
      {open && (
        <div className="absolute left-0 top-full z-50 mt-1 min-w-full overflow-hidden rounded-xl border border-slate-200 bg-white shadow-xl dark:border-slate-700 dark:bg-slate-800">
          {/* Limit to 6 visible rows (~35px each) */}
          <div className="max-h-[210px] overflow-y-auto">
            {options.map((option) => (
              <button
                key={option.id}
                type="button"
                onClick={() => { onChange(option.id || null); setOpen(false); }}
                className={[
                  'flex w-full items-center truncate px-3 py-2 text-[13px] transition hover:bg-slate-50 dark:hover:bg-slate-700',
                  option.id === value
                    ? 'font-semibold text-blue-600 dark:text-blue-400'
                    : 'text-slate-700 dark:text-slate-200',
                ].join(' ')}
              >
                {option.id === value && (
                  <span className="mr-2 h-1.5 w-1.5 shrink-0 rounded-full bg-blue-600 dark:bg-blue-400" />
                )}
                <span className="truncate">{option.name}</span>
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  );
};

export const Layout = ({ children }: { children: ReactNode }) => {
  const [isSidebarOpen, setIsSidebarOpen] = useState(false);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(
    () => localStorage.getItem('clear-sidebar-collapsed') === 'true'
  );
  const [isThemeOpen, setIsThemeOpen] = useState(false);
  const [isNotifOpen, setIsNotifOpen] = useState(false);
  const themeRef = useRef<HTMLDivElement>(null);
  const notifRef = useRef<HTMLDivElement>(null);
  const { user, logout } = useAuth();
  const { projects, projectId, setProjectId } = useProjectScope();
  const { theme, set: setTheme } = useTheme();
  const { pathname } = useLocation();

  const toggleSidebarCollapse = () => {
    setSidebarCollapsed((prev) => {
      const next = !prev;
      localStorage.setItem('clear-sidebar-collapsed', String(next));
      return next;
    });
  };

  // Close the panel when clicking outside it
  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (themeRef.current && !themeRef.current.contains(e.target as Node)) {
        setIsThemeOpen(false);
      }
      if (notifRef.current && !notifRef.current.contains(e.target as Node)) {
        setIsNotifOpen(false);
      }
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, []);

  return (
    <div className="min-h-screen bg-slate-50 dark:bg-[#0f172a]">
      <Sidebar
        isOpen={isSidebarOpen}
        onClose={() => setIsSidebarOpen(false)}
        isCollapsed={sidebarCollapsed}
        onToggleCollapse={toggleSidebarCollapse}
      />

      <div className={`transition-[padding] duration-300 ${sidebarCollapsed ? 'lg:pl-16' : 'lg:pl-64'}`}>
        <header className="sticky top-0 z-20 border-b border-[#E4E7EC] bg-white dark:border-slate-700 dark:bg-slate-900">
          <div className="flex h-[60px] items-center justify-between gap-3 px-4 sm:gap-4 sm:px-6 lg:px-8">
            <div className="flex min-w-0 items-center gap-3">
              <button
                type="button"
                onClick={() => setIsSidebarOpen(true)}
                title="Open navigation"
                aria-label="Open navigation"
                className="shrink-0 rounded-xl border border-slate-200 p-2 text-slate-600 shadow-sm transition hover:bg-slate-50 dark:border-slate-700 dark:text-slate-300 dark:hover:bg-slate-700 lg:hidden"
              >
                <Menu className="h-5 w-5" />
              </button>
              <div className="min-w-0">
                <p className="hidden font-mono text-[10px] font-semibold uppercase tracking-[1.2px] text-[#94A0B4] sm:block">
                  Workspace
                </p>
                <h2 className="truncate text-[18px] font-semibold text-[#0F172A] dark:text-slate-100 sm:text-[20px]">
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
                <ProjectDropdown
                  value={projectId ?? ''}
                  onChange={setProjectId}
                  projects={projects}
                  className="w-40 lg:w-48"
                />
              </label>

              {/* Notification bell */}
              <div ref={notifRef} className="relative">
                <button
                  type="button"
                  onClick={() => setIsNotifOpen((o) => !o)}
                  aria-label="Notifications"
                  className="relative flex h-8 w-8 shrink-0 items-center justify-center rounded-full border border-slate-200 bg-white text-slate-500 shadow-sm transition hover:bg-slate-50 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-300 dark:hover:bg-slate-700 sm:h-9 sm:w-9"
                >
                  <Bell className="h-4 w-4" />
                  {/* Unread badge */}
                  {NOTIFICATIONS.some((n) => !n.read) && (
                    <span className="absolute right-1.5 top-1.5 h-2 w-2 rounded-full bg-rose-500 ring-2 ring-white dark:ring-slate-800" />
                  )}
                </button>

                {isNotifOpen && (
                  <div className="absolute right-0 top-full z-50 mt-2 w-80 overflow-hidden rounded-xl border border-slate-200 bg-white shadow-xl dark:border-slate-700 dark:bg-slate-800">
                    <div className="flex items-center justify-between border-b border-slate-100 px-4 py-3 dark:border-slate-700">
                      <p className="text-[13px] font-semibold text-slate-800 dark:text-slate-100">Notifications</p>
                      <span className="rounded-full bg-rose-100 px-2 py-0.5 text-[11px] font-semibold text-rose-600 dark:bg-rose-950 dark:text-rose-400">
                        {NOTIFICATIONS.filter((n) => !n.read).length} new
                      </span>
                    </div>
                    <div className="max-h-[340px] overflow-y-auto divide-y divide-slate-100 dark:divide-slate-700">
                      {NOTIFICATIONS.map((notif) => (
                        <div
                          key={notif.id}
                          className={[
                            'flex gap-3 px-4 py-3 transition hover:bg-slate-50 dark:hover:bg-slate-700/50',
                            !notif.read ? 'bg-blue-50/60 dark:bg-blue-950/20' : '',
                          ].join(' ')}
                        >
                          <span className={[
                            'mt-1.5 h-2 w-2 shrink-0 rounded-full',
                            !notif.read ? 'bg-blue-500' : 'bg-slate-300 dark:bg-slate-600',
                          ].join(' ')} />
                          <div className="min-w-0">
                            <p className="text-[12px] font-semibold text-slate-800 dark:text-slate-100">{notif.title}</p>
                            <p className="mt-0.5 text-[11px] text-slate-500 dark:text-slate-400">{notif.message}</p>
                            <p className="mt-1 text-[10px] font-medium text-slate-400 dark:text-slate-500">{notif.time}</p>
                          </div>
                        </div>
                      ))}
                    </div>
                    <div className="border-t border-slate-100 px-4 py-2.5 dark:border-slate-700">
                      <button type="button" className="w-full text-center text-[12px] font-medium text-blue-600 transition hover:text-blue-700 dark:text-blue-400 dark:hover:text-blue-300">
                        Mark all as read
                      </button>
                    </div>
                  </div>
                )}
              </div>

              <div ref={themeRef} className="relative">
                <button
                  type="button"
                  onClick={() => setIsThemeOpen((o) => !o)}
                  aria-label="Open appearance menu"
                  className="flex h-8 w-8 shrink-0 cursor-pointer items-center justify-center rounded-full bg-[#2563EB] text-xs font-semibold text-white transition hover:opacity-90 sm:h-9 sm:w-9"
                >
                  {initialsOf(user?.full_name) || 'XX'}
                </button>

                {isThemeOpen && (
                  <div className="absolute right-0 top-full z-50 mt-2 w-44 overflow-hidden rounded-xl border border-slate-200 bg-white shadow-lg dark:border-slate-700 dark:bg-slate-800">
                    <div className="border-b border-slate-100 px-3 py-2.5 dark:border-slate-700">
                      <p className="text-[12px] font-semibold text-slate-800 dark:text-slate-100">{user?.full_name}</p>
                      <p className="truncate text-[11px] text-slate-400 dark:text-slate-500">{user?.email}</p>
                    </div>
                    <p className="border-b border-slate-100 px-3 py-2 text-[10px] font-semibold uppercase tracking-widest text-slate-400 dark:border-slate-700 dark:text-slate-500">
                      Appearance
                    </p>
                    <button
                      type="button"
                      onClick={() => { setTheme('light'); }}
                      className={[
                        'flex w-full items-center gap-2.5 px-3 py-2.5 text-[13px] font-medium transition hover:bg-slate-50 dark:hover:bg-slate-700',
                        theme === 'light' ? 'text-blue-600 dark:text-blue-400' : 'text-slate-700 dark:text-slate-300',
                      ].join(' ')}
                    >
                      <Sun className="h-4 w-4 shrink-0" />
                      Light
                      {theme === 'light' && <span className="ml-auto h-1.5 w-1.5 rounded-full bg-blue-600 dark:bg-blue-400" />}
                    </button>
                    <button
                      type="button"
                      onClick={() => { setTheme('dark'); }}
                      className={[
                        'flex w-full items-center gap-2.5 px-3 py-2.5 text-[13px] font-medium transition hover:bg-slate-50 dark:hover:bg-slate-700',
                        theme === 'dark' ? 'text-blue-600 dark:text-blue-400' : 'text-slate-700 dark:text-slate-300',
                      ].join(' ')}
                    >
                      <Moon className="h-4 w-4 shrink-0" />
                      Dark
                      {theme === 'dark' && <span className="ml-auto h-1.5 w-1.5 rounded-full bg-blue-600 dark:bg-blue-400" />}
                    </button>
                    <div className="border-t border-slate-100 dark:border-slate-700">
                      <button
                        type="button"
                        onClick={() => void logout()}
                        className="flex w-full items-center gap-2.5 px-3 py-2.5 text-[13px] font-medium text-rose-500 transition hover:bg-rose-50 dark:text-rose-400 dark:hover:bg-rose-950/40"
                      >
                        Sign out
                      </button>
                    </div>
                  </div>
                )}
              </div>
            </div>
          </div>

          {/* Below `md` the project scope moves under the title so it is still
              reachable without a horizontally cramped header row. */}
          <div className="border-t border-[#E4E7EC] px-4 py-2 dark:border-slate-700 md:hidden">
            <ProjectDropdown
              value={projectId ?? ''}
              onChange={setProjectId}
              projects={projects}
              placeholder="All my business units"
              className="w-full"
            />
          </div>
        </header>

        <main className="px-4 py-4 sm:px-6 sm:py-6 lg:px-8">{children}</main>
      </div>
    </div>
  );
};

export default Layout;
