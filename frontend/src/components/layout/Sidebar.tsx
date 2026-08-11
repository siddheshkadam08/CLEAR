/**
 * Fixed 256px sidebar, collapsible below `lg`.
 *
 * The list, its order and the rules for who sees what all live in `./navigation`,
 * which the top bar reads too. A screen named here and again there is a screen
 * that will be renamed in one of them.
 */

import { ChevronLeft, ChevronRight, X } from 'lucide-react';
import { Link, NavLink, useLocation } from 'react-router-dom';

import { useAuth } from '@/lib/auth';
import { initialsOf } from '@/lib/identity';
import { NAV_ROUTES, navLabelOf, visibleTo } from './navigation';
import type { NavRoute } from './navigation';

const ClearLogo = ({ size = 50, className }: { size?: number; className?: string }) => (
  <img
    src="/image/irisclear.png"
    alt="C.L.E.A.R"
    width={size}
    height={size}
    className={className}
    style={{ objectFit: 'contain' }}
  />
);

const NavItemLink = ({
  item,
  pathname,
  onNavigate,
  isCollapsed,
}: {
  item: NavRoute;
  pathname: string;
  onNavigate: () => void;
  isCollapsed: boolean;
}) => {
  const { path, nav } = item;
  const Icon = nav.icon;
  const label = navLabelOf(item);
  const active = path === '/' ? pathname === '/' : pathname.startsWith(path);

  return (
    <div className="group/item relative">
      <NavLink
        to={path}
        onClick={onNavigate}
        className={[
          'flex items-center rounded-lg transition-colors',
          isCollapsed ? 'justify-center p-3' : 'gap-2.5 px-2.5 py-2 text-sm font-medium',
          active
            ? 'bg-[#2563EB] text-white'
            : 'text-[#8B96AC] hover:bg-white/[0.05] hover:text-[#E7EAF0]',
        ].join(' ')}
      >
        <Icon
          className={[
            'shrink-0',
            isCollapsed ? 'h-5 w-5' : 'h-4 w-4',
            active ? 'text-white' : 'text-[#8B96AC] group-hover/item:text-[#E7EAF0]',
          ].join(' ')}
        />
        {!isCollapsed && <span className="truncate">{label}</span>}
      </NavLink>
      {/* Tooltip shown only when sidebar is collapsed */}
      {isCollapsed && (
        <div className="pointer-events-none absolute left-full top-1/2 z-[60] ml-3 -translate-y-1/2 whitespace-nowrap rounded-lg bg-slate-700 px-2.5 py-1.5 text-[12px] font-medium text-white opacity-0 shadow-xl transition-opacity group-hover/item:opacity-100">
          {label}
          <div className="absolute -left-1 top-1/2 h-2 w-2 -translate-y-1/2 rotate-45 bg-slate-700" />
        </div>
      )}
    </div>
  );
};

export const Sidebar = ({
  isOpen,
  onClose,
  isCollapsed = false,
  onToggleCollapse,
}: {
  isOpen: boolean;
  onClose: () => void;
  isCollapsed?: boolean;
  onToggleCollapse?: () => void;
}) => {
  const { user } = useAuth();
  const { pathname } = useLocation();

  // Role-gated items are filtered out, never rendered-then-disabled: a control you
  // cannot use is noise, and a greyed-out admin link tells an ordinary user that
  // something exists which is none of their business.
  const isAdmin = Boolean(user?.is_system_admin);
  // Flattened across every membership: these items span projects, so holding the
  // permission anywhere is what decides whether the screen is offered. The
  // endpoint behind it still scopes rows to the caller's own projects.
  const permissions = new Set(
    (user?.memberships ?? []).flatMap((membership) => membership.permissions),
  );
  const items = NAV_ROUTES.filter((route) => visibleTo(route.nav, isAdmin, permissions));
  const primary = items.filter((route) => !route.nav.section);
  const governance = items.filter((route) => route.nav.section);
  const governanceHeading = governance[0]?.nav.section;

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
          'fixed inset-y-0 left-0 z-40 flex flex-col bg-[#0F172A] shadow-xl transition-all duration-300 ease-in-out lg:translate-x-0 lg:shadow-none',
          isOpen ? 'translate-x-0' : '-translate-x-full',
          isCollapsed ? 'w-64 lg:w-16' : 'w-64',
        ].join(' ')}
      >
        {isCollapsed ? (
          // Collapsed header: centered logo link above the expand button
          <div className="flex flex-col items-center gap-1 py-3">
            <Link to="/" onClick={onClose} className="rounded-lg p-1 transition hover:bg-white/10">
              <ClearLogo size={48} />
            </Link>
            <button
              type="button"
              onClick={onToggleCollapse}
              className="hidden rounded-lg p-1.5 text-[#8B96AC] transition hover:bg-white/10 hover:text-[#E7EAF0] lg:flex"
            >
              <ChevronRight className="h-4 w-4" />
            </button>
          </div>
        ) : (
          // Expanded header: logo + name on left, collapse/close button on right
          <div className="flex items-center justify-between px-4 py-4">
            <Link to="/" onClick={onClose} className="flex min-w-0 items-center gap-2.5">
              <ClearLogo size={170} className="shrink-0" />
            </Link>
            <div className="flex shrink-0 items-center gap-1">
              <button
                type="button"
                onClick={onToggleCollapse}
                className="hidden rounded-lg p-1.5 text-[#8B96AC] transition hover:bg-white/10 hover:text-[#E7EAF0] lg:flex"
              >
                <ChevronLeft className="h-4 w-4" />
              </button>
              <button
                type="button"
                onClick={onClose}
                title="Close navigation"
                className="rounded-lg p-1.5 text-[#8B96AC] transition hover:bg-white/10 hover:text-[#E7EAF0] lg:hidden"
              >
                <X className="h-4 w-4" />
              </button>
            </div>
          </div>
        )}

        <nav className={`flex-1 px-3 py-4${!isCollapsed ? ' overflow-y-auto' : ''}`}>
          <div className="space-y-0.5">
            {primary.map((item) => (
              <NavItemLink
                key={item.path}
                item={item}
                pathname={pathname}
                onNavigate={onClose}
                isCollapsed={isCollapsed}
              />
            ))}
          </div>

          {governanceHeading ? (
            <div className="mt-5 space-y-0.5">
              {!isCollapsed && (
                <p className="px-2.5 pb-2 text-[10px] font-semibold uppercase tracking-[1.2px] text-[#94A0B4]">
                  {governanceHeading}
                </p>
              )}
              {isCollapsed && <div className="mx-auto mb-2 h-px w-8 bg-white/10" />}
              {governance.map((item) => (
                <NavItemLink
                  key={item.path}
                  item={item}
                  pathname={pathname}
                  onNavigate={onClose}
                  isCollapsed={isCollapsed}
                />
              ))}
            </div>
          ) : null}
        </nav>

        <div className="border-t border-white/10 p-3">
          {isCollapsed ? (
            <div className="flex flex-col items-center gap-2">
              {/* User avatar with name+email tooltip */}
              <div className="group/userinfo relative">
                <div className="flex h-8 w-8 cursor-default items-center justify-center rounded-full bg-[#2563EB] text-xs font-semibold text-white">
                  {initialsOf(user?.full_name) || 'XX'}
                </div>
                <div className="pointer-events-none absolute left-full top-1/2 z-[60] ml-3 min-w-[160px] -translate-y-1/2 rounded-lg bg-slate-700 px-3 py-2.5 opacity-0 shadow-xl transition-opacity group-hover/userinfo:opacity-100">
                  <p className="text-[12px] font-semibold text-white">{user?.full_name ?? 'Guest User'}</p>
                  <p className="mt-0.5 truncate text-[11px] text-slate-400">{user?.email ?? ''}</p>
                  <div className="absolute -left-1 top-1/2 h-2 w-2 -translate-y-1/2 rotate-45 bg-slate-700" />
                </div>
              </div>
              {/* Sign-out with styled tooltip */}
              </div>
          ) : (
            <>
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
            </>
          )}
        </div>
      </aside>
    </>
  );
};

export default Sidebar;
