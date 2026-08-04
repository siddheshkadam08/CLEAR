/**
 * Authentication state.
 *
 * The access token lives in the API client's module scope, never in this store and
 * never in localStorage - see `api/client.ts`. This store holds only *who* is
 * signed in, which is safe to keep in memory and cheap to re-derive on reload from
 * the HttpOnly refresh cookie.
 */

import { create } from 'zustand';

import { auth as authApi } from '@/api/endpoints';
import { setAccessToken } from '@/api/client';
import { api } from '@/api/client';
import type { CurrentUser } from '@/api/types';

interface AuthState {
  user: CurrentUser | null;
  /** True until the initial session restore finishes, so the router can wait. */
  initialising: boolean;
  error: string | null;

  login: (email: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  /** Recover a session on page load using the refresh cookie. */
  restore: () => Promise<void>;
}

export const useAuth = create<AuthState>((set) => ({
  user: null,
  initialising: true,
  error: null,

  login: async (email, password) => {
    set({ error: null });
    const tokens = await authApi.login(email, password);
    setAccessToken(tokens.access_token);
    // The login response carries the user, so signing in is one round trip.
    set({ user: tokens.user, error: null });
  },

  logout: async () => {
    try {
      await authApi.logout();
    } finally {
      // Cleared even if the call failed: the user asked to be signed out, and
      // leaving a usable token behind because the server was unreachable is the
      // wrong way to fail.
      setAccessToken(null);
      set({ user: null });
    }
  },

  restore: async () => {
    try {
      const refreshed = await api.refresh();
      if (!refreshed) {
        set({ user: null, initialising: false });
        return;
      }
      const user = await authApi.me();
      set({ user, initialising: false });
    } catch {
      set({ user: null, initialising: false });
    }
  },
}));

/** Does the signed-in user hold this permission? System admins hold all of them. */
export function useCanAdminister(): boolean {
  return useAuth((state) => state.user?.is_system_admin ?? false);
}

/**
 * Does the user hold this permission on *any* project?
 *
 * For screens that span projects, where there is no single project to check
 * against. `memberships[].permissions` is resolved server-side per request, so
 * this reads the real grant rather than inferring one from a role name.
 *
 * Presentation only - it decides whether to offer a link. The endpoint behind the
 * screen enforces the same permission and scopes rows to the caller's projects,
 * so a stale membership list can hide a screen but never open one.
 */
export function useHasPermissionAnywhere(permission: string): boolean {
  return useAuth((state) => {
    const user = state.user;
    if (!user) return false;
    if (user.is_system_admin) return true;
    return user.memberships.some((membership) =>
      membership.permissions.includes(permission),
    );
  });
}
