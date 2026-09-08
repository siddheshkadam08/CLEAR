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

const SYSTEM_DARK = '(prefers-color-scheme: dark)';

/** The browser's current preference. */
function systemTheme(): Theme {
  return window.matchMedia(SYSTEM_DARK).matches ? 'dark' : 'light';
}

/**
 * Whether the user has chosen a theme explicitly.
 *
 * An explicit choice outranks the browser: someone who set light on a dark
 * desktop meant it, and having the app quietly overrule them is worse than not
 * following the system at all.
 */
function hasExplicitChoice(): boolean {
  const stored = localStorage.getItem('clear-theme');
  return stored === 'dark' || stored === 'light';
}

function getInitialTheme(): Theme {
  const stored = localStorage.getItem('clear-theme') as Theme | null;
  if (stored === 'dark' || stored === 'light') return stored;
  return systemTheme();
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

/**
 * Follow the browser while it is still the one deciding.
 *
 * Without this the system preference was read once, at load. Switching the OS or
 * browser to dark with the app already open left it in light mode until a
 * reload - and a long-lived tab is exactly where that happens.
 *
 * Guarded on `hasExplicitChoice` so the toggle keeps winning: once someone picks
 * a theme, the system stops moving it.
 */
if (typeof window !== 'undefined' && window.matchMedia) {
  const query = window.matchMedia(SYSTEM_DARK);
  const follow = (event: MediaQueryListEvent) => {
    if (hasExplicitChoice()) return;
    const next: Theme = event.matches ? 'dark' : 'light';
    applyTheme(next);
    useTheme.setState({ theme: next });
  };
  // `addEventListener` on a MediaQueryList is unsupported in older WebKit, which
  // only has the deprecated `addListener`. Feature-detected rather than assumed,
  // because the failure is a TypeError at module load - a blank app, not a
  // theme that lags.
  if (typeof query.addEventListener === 'function') {
    query.addEventListener('change', follow);
  } else if (typeof query.addListener === 'function') {
    query.addListener(follow);
  }
}
