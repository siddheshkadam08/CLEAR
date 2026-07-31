/**
 * Fixed 256px sidebar, collapsible below `lg`.
 *
 * Nav is ordered by workflow rather than alphabetically: upload a contract, review
 * what came out, ask questions across the repository, then watch the machinery and
 * configure it. That order is the product's own story.
 *
 * Two audiences see two different lists. An administrator governs the platform -
 * projects, people, master data - and reads everything, but does not put contracts
 * into it; a project member does the contract work. `Upload` is therefore absent
 * for an administrator rather than present-and-rejected, which would advertise a
 * screen whose every submission returns 403.
 */

import {
  AlertTriangle,
  Bot,
  FileSearch,
  FileText,
  FolderKanban,
  LayoutDashboard,
  ListChecks,
  LogOut,
  Search,
  Settings2,
  Upload,
  Users,
  X,
} from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import { NavLink, useLocation } from 'react-router-dom';

import { useAuth } from '@/lib/auth';
import { APP_NAME, initialsOf } from '@/lib/identity';

/** Who a nav item is for. `member` means "everyone except the administrator". */
type Audience = 'all' | 'admin' | 'member';

interface NavItem {
  href: string;
  label: string;
  icon: LucideIcon;
  audience?: Audience;
  section?: string;
}

const NAV: NavItem[] = [
  { href: '/', label: 'Dashboard', icon: LayoutDashboard },
  { href: '/upload', label: 'Upload', icon: Upload, audience: 'member' },
  { href: '/contracts', label: 'Contracts', icon: FileText },
  { href: '/search', label: 'Search', icon: Search },
  { href: '/copilot', label: 'Copilot', icon: Bot },
  { href: '/jobs', label: 'Processing', icon: ListChecks },
  { href: '/doc-pipeline', label: 'Doc Pipeline', icon: FileSearch },
  { href: '/alerts', label: 'Alerts', icon: AlertTriangle },
  {
    href: '/admin/projects',
    label: 'Projects',
    icon: FolderKanban,
    audience: 'admin',
    section: 'Administration',
  },
  {
    href: '/admin/users',
    label: 'Users',
    icon: Users,
    audience: 'admin',
    section: 'Administration',
  },
  {
    href: '/clause-master',
    label: 'Clause Master',
    icon: Settings2,
    audience: 'admin',
    section: 'Administration',
  },
];

const visibleTo = (item: NavItem, isAdmin: boolean) =>
  item.audience === 'admin' ? isAdmin : item.audience === 'member' ? !isAdmin : true;

const NavItemLink = ({
  item,
  pathname,
  onNavigate,
}: {
  item: NavItem;
  pathname: string;
  onNavigate: () => void;
}) => {
  const { href, label, icon: Icon } = item;
  // Prefix matching everywhere except the index route, which would otherwise be
  // "active" on every page in the app.
  const active = href === '/' ? pathname === '/' : pathname.startsWith(href);

  return (
    <NavLink
      to={href}
      onClick={onNavigate}
      className={[
        'group flex items-center gap-3 rounded-xl px-3 py-2.5 text-sm font-medium transition-colors',
        active
          ? 'bg-blue-50 text-blue-700 shadow-sm ring-1 ring-blue-100'
          : 'text-slate-600 hover:bg-slate-100 hover:text-slate-900',
      ].join(' ')}
    >
      <Icon
        className={[
          'h-5 w-5 shrink-0',
          active ? 'text-blue-600' : 'text-slate-400 group-hover:text-slate-700',
        ].join(' ')}
      />
      <span className="truncate">{label}</span>
    </NavLink>
  );
};

export const Sidebar = ({ isOpen, onClose }: { isOpen: boolean; onClose: () => void }) => {
  const { user, logout } = useAuth();
  const { pathname } = useLocation();

  // Role-gated items are filtered out, never rendered-then-disabled: a control you
  // cannot use is noise, and a greyed-out admin link tells an ordinary user that
  // something exists which is none of their business.
  const isAdmin = Boolean(user?.is_system_admin);
  const items = NAV.filter((item) => visibleTo(item, isAdmin));
  const primary = items.filter((item) => !item.section);
  const governance = items.filter((item) => item.section);
  const governanceHeading = governance[0]?.section;

  return (
    <>
      <div
        className={[
          'fixed inset-0 z-30 bg-slate-950/40 transition-opacity lg:hidden',
          isOpen ? 'opacity-100' : 'pointer-events-none opacity-0',
        ].join(' ')}
        onClick={onClose}
      />

      <aside
        className={[
          'fixed inset-y-0 left-0 z-40 flex w-64 flex-col border-r border-slate-200 bg-white shadow-xl transition-transform lg:translate-x-0 lg:shadow-none',
          isOpen ? 'translate-x-0' : '-translate-x-full',
        ].join(' ')}
      >
        <div className="flex items-center justify-between border-b border-slate-200 px-5 py-5">
          <div>
            <p className="text-xs font-semibold uppercase tracking-[0.2em] text-blue-600">
              AI-Powered
            </p>
            <h1 className="mt-1 text-xl font-semibold text-slate-900">{APP_NAME}</h1>
          </div>
          <button
            type="button"
            onClick={onClose}
            title="Close navigation"
            className="rounded-lg p-2 text-slate-500 transition hover:bg-slate-100 hover:text-slate-900 lg:hidden"
          >
            <X className="h-5 w-5" />
          </button>
        </div>

        <nav className="flex-1 overflow-y-auto px-3 py-5">
          <div className="space-y-1">
            {primary.map((item) => (
              <NavItemLink
                key={item.href}
                item={item}
                pathname={pathname}
                onNavigate={onClose}
              />
            ))}
          </div>

          {governanceHeading ? (
            <div className="mt-6 space-y-1">
              <p className="px-3 pb-2 text-xs font-semibold uppercase tracking-[0.16em] text-slate-400">
                {governanceHeading}
              </p>
              {governance.map((item) => (
                <NavItemLink
                  key={item.href}
                  item={item}
                  pathname={pathname}
                  onNavigate={onClose}
                />
              ))}
            </div>
          ) : null}
        </nav>

        <div className="border-t border-slate-200 p-4">
          <div className="mb-3 flex items-center gap-3 rounded-2xl bg-slate-50 p-3">
            <div className="flex h-11 w-11 shrink-0 items-center justify-center rounded-full bg-blue-600 text-sm font-semibold text-white">
              {initialsOf(user?.full_name) || 'XX'}
            </div>
            <div className="min-w-0">
              <p className="truncate text-sm font-semibold text-slate-900">
                {user?.full_name ?? 'Guest User'}
              </p>
              <p className="truncate text-xs text-slate-500">
                {user?.email ?? 'Not signed in'}
              </p>
            </div>
          </div>
          <button
            type="button"
            onClick={() => void logout()}
            className="flex w-full items-center justify-center gap-2 rounded-xl border border-slate-200 px-4 py-2.5 text-sm font-medium text-slate-700 transition hover:border-slate-300 hover:bg-slate-100"
          >
            <LogOut className="h-4 w-4" /> Logout
          </button>
        </div>
      </aside>
    </>
  );
};

export default Sidebar;
