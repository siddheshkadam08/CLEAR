/**
 * Project scope - the selected project, shared across every screen.
 *
 * `null` means "every project I belong to", which is what the backend already
 * enforces (§1.1). The selector narrows that set; it can never widen it, because
 * the server resolves scope from membership and ignores anything the client asks
 * for beyond it.
 */

import { useQuery } from '@tanstack/react-query';
import { create } from 'zustand';

import { projects as projectsApi } from '@/api/endpoints';
import type { ProjectListItem, UUID } from '@/api/types';

interface ScopeState {
  projectId: UUID | null;
  setProjectId: (id: UUID | null) => void;
}

const STORAGE_KEY = 'cip.project';

const useScopeStore = create<ScopeState>((set) => ({
  // Restored from localStorage: a project selection is a UI preference, not a
  // credential, and losing it on every reload is needless friction.
  projectId: localStorage.getItem(STORAGE_KEY),
  setProjectId: (id) => {
    if (id) localStorage.setItem(STORAGE_KEY, id);
    else localStorage.removeItem(STORAGE_KEY);
    set({ projectId: id });
  },
}));

export function useProjectScope() {
  const { projectId, setProjectId } = useScopeStore();
  const { data } = useQuery({
    queryKey: ['projects'],
    // Every project, not the server's default first page of 25. The selector is
    // a complete list or it is wrong twice over: a user in more than 25 projects
    // could not choose the ones past the cap, and - worse - the reset below
    // would read their stored selection as stale and clear it, *widening* their
    // view from one project to all of them.
    queryFn: () => projectsApi.list({ size: 200 }),
    staleTime: 5 * 60_000,
  });

  const list: ProjectListItem[] = data?.items ?? [];

  // A stale stored id - a project the user was removed from - must not silently
  // filter every screen to nothing.
  const resolved = projectId && list.some((p) => p.id === projectId) ? projectId : null;

  return { projects: list, projectId: resolved, setProjectId };
}
