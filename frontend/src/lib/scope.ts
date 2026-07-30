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
    queryFn: () => projectsApi.list(),
    staleTime: 5 * 60_000,
  });

  const list: ProjectListItem[] = data?.items ?? [];

  // A stale stored id - a project the user was removed from - must not silently
  // filter every screen to nothing.
  const resolved = projectId && list.some((p) => p.id === projectId) ? projectId : null;

  return { projects: list, projectId: resolved, setProjectId };
}
