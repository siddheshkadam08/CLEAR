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
  const active = href === '/' ? pathname === '/' : pathname.startsWith(href);

  return (
    <NavLink
      to={href}
      onClick={onNavigate}
      className={[
        'group flex items-center gap-2.5 rounded-lg px-2.5 py-2 text-sm font-medium transition-colors',
        active
          ? 'bg-[#2563EB] text-white'
          : 'text-[#8B96AC] hover:bg-white/[0.05] hover:text-[#E7EAF0]',
      ].join(' ')}
    >
      <Icon
        className={[
          'h-4 w-4 shrink-0',
          active ? 'text-white' : 'text-[#8B96AC] group-hover:text-[#E7EAF0]',
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
          'fixed inset-y-0 left-0 z-40 flex w-64 flex-col bg-[#0F172A] shadow-xl transition-transform lg:translate-x-0 lg:shadow-none',
          isOpen ? 'translate-x-0' : '-translate-x-full',
        ].join(' ')}
      >
        <div className="flex items-center justify-between px-5 py-5 pb-4">
          <div className="flex items-center gap-2.5">
            <svg width="28" height="28" viewBox="0 0 40 40" fill="none" className="shrink-0">
              <circle cx="20" cy="20" r="16.5" stroke="#2563EB" strokeWidth="3.4" strokeDasharray="72 32" strokeLinecap="round" transform="rotate(-90 20 20)" />
              <rect x="15.5" y="11" width="11" height="18" rx="2.2" fill="#E7EAF0" />
              <rect x="18" y="16" width="6" height="1.6" rx="0.8" fill="#0F172A" />
              <rect x="18" y="20" width="6" height="1.6" rx="0.8" fill="#0F172A" />
              <rect x="18" y="24" width="4" height="1.6" rx="0.8" fill="#0F172A" />
            </svg>
            <span className="text-[17px] font-bold tracking-[0.4px] text-[#E7EAF0]">{APP_NAME}</span>
          </div>
          <button
            type="button"
            onClick={onClose}
            title="Close navigation"
            className="rounded-lg p-1.5 text-[#8B96AC] transition hover:bg-white/10 hover:text-[#E7EAF0] lg:hidden"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <nav className="flex-1 overflow-y-auto px-3 py-4">
          <div className="space-y-0.5">
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
            <div className="mt-5 space-y-0.5">
              <p className="px-2.5 pb-2 text-[10px] font-semibold uppercase tracking-[1.2px] text-[#94A0B4]">
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

        <div className="border-t border-white/10 p-3">
          <div className="mb-2.5 flex items-center gap-2.5 rounded-lg bg-white/5 p-2.5">
            <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-[#2563EB] text-xs font-semibold text-white">
              {initialsOf(user?.full_name) || 'XX'}
            </div>
            <div className="min-w-0">
              <p className="truncate text-sm font-semibold text-[#E7EAF0]">
                {user?.full_name ?? 'Guest User'}
              </p>
              <p className="truncate text-xs text-[#8B96AC]">
                {user?.email ?? 'Not signed in'}
              </p>
            </div>
          </div>
          <button
            type="button"
            onClick={() => void logout()}
            className="flex w-full items-center justify-center gap-2 rounded-lg border border-white/10 px-3 py-2 text-xs font-medium text-[#8B96AC] transition hover:border-white/20 hover:bg-white/5 hover:text-[#E7EAF0]"
          >
            <LogOut className="h-3.5 w-3.5" /> Sign out
          </button>
        </div>
      </aside>
    </>
  );
};

export default Sidebar;
