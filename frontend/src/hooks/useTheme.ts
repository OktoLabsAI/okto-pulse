import { useCallback, useEffect, useSyncExternalStore } from 'react';

type Theme = 'light' | 'dark';

const STORAGE_KEY = 'omniflow-theme';

function getSystemTheme(): Theme {
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

function getStoredTheme(): Theme | null {
  try {
    const v = localStorage.getItem(STORAGE_KEY);
    return v === 'light' || v === 'dark' ? v : null;
  } catch {
    return null;
  }
}

function resolveTheme(): Theme {
  return getStoredTheme() ?? getSystemTheme();
}

function applyTheme(theme: Theme) {
  document.documentElement.classList.toggle('dark', theme === 'dark');
}

// Tiny pub/sub so every useTheme hook re-renders on change
let listeners: Array<() => void> = [];
let currentTheme: Theme = resolveTheme();

function subscribe(cb: () => void) {
  listeners.push(cb);
  return () => { listeners = listeners.filter((l) => l !== cb); };
}

function getSnapshot(): Theme {
  return currentTheme;
}

function setTheme(theme: Theme) {
  currentTheme = theme;
  localStorage.setItem(STORAGE_KEY, theme);
  applyTheme(theme);
  listeners.forEach((l) => l());
}

// Apply on module load (before first paint)
applyTheme(currentTheme);

// Listen to OS theme changes (only matters when no stored preference)
if (typeof window !== 'undefined') {
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
    if (!getStoredTheme()) {
      setTheme(getSystemTheme());
    }
  });
}

export function useTheme() {
  const theme = useSyncExternalStore(subscribe, getSnapshot);

  // Ensure class is in sync on mount
  useEffect(() => applyTheme(theme), [theme]);

  const toggle = useCallback(() => {
    setTheme(currentTheme === 'dark' ? 'light' : 'dark');
  }, []);

  return { theme, toggle, setTheme } as const;
}
