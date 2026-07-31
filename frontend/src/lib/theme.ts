import { create } from 'zustand';

type Theme = 'light' | 'dark';

interface ThemeStore {
  theme: Theme;
  toggle: () => void;
  set: (theme: Theme) => void;
}

function applyTheme(theme: Theme) {
  const root = document.documentElement;
  if (theme === 'dark') {
    root.classList.add('dark');
  } else {
    root.classList.remove('dark');
  }
}

function getInitialTheme(): Theme {
  const stored = localStorage.getItem('clear-theme') as Theme | null;
  if (stored === 'dark' || stored === 'light') return stored;
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

export const useTheme = create<ThemeStore>((set, get) => ({
  theme: getInitialTheme(),
  toggle: () => {
    const next = get().theme === 'dark' ? 'light' : 'dark';
    localStorage.setItem('clear-theme', next);
    applyTheme(next);
    set({ theme: next });
  },
  set: (theme: Theme) => {
    localStorage.setItem('clear-theme', theme);
    applyTheme(theme);
    set({ theme });
  },
}));

// Apply on load
applyTheme(getInitialTheme());
