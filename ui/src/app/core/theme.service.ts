import { Injectable, effect, signal } from '@angular/core';

/**
 * User-selectable theme preference.
 *
 * ``"system"`` defers to the OS ``prefers-color-scheme`` and is the default
 * when nothing is persisted.
 */
export type Theme = 'light' | 'dark' | 'system';

/** Concrete theme actually applied to the document. */
export type ResolvedTheme = 'light' | 'dark';

const STORAGE_KEY = 'theme';
const DARK_QUERY = '(prefers-color-scheme: dark)';

function prefersDark(): boolean {
  return typeof window !== 'undefined' && window.matchMedia(DARK_QUERY).matches;
}

/** Read the persisted preference, defaulting to ``"system"``. */
function readStoredTheme(): Theme {
  try {
    const value = localStorage.getItem(STORAGE_KEY);
    if (value === 'light' || value === 'dark') {
      return value;
    }
  } catch {
    /* localStorage unavailable (private mode); fall through to default. */
  }
  return 'system';
}

/** Resolve a preference to the concrete light/dark theme to apply. */
function resolveTheme(theme: Theme): ResolvedTheme {
  if (theme === 'system') {
    return prefersDark() ? 'dark' : 'light';
  }
  return theme;
}

/**
 * Manage the application theme.
 *
 * Persists the user's explicit ``"light"``/``"dark"`` choice in
 * ``localStorage`` and removes it when they return to ``"system"`` so the app
 * follows the OS again. Keeps the document ``.dark`` class in sync (via an
 * ``effect``) and reacts to OS changes while in ``"system"`` mode.
 */
@Injectable({ providedIn: 'root' })
export class ThemeService {
  /** The current user preference (light / dark / system). */
  readonly theme = signal<Theme>(readStoredTheme());

  /** The concrete theme applied to the document. */
  readonly resolvedTheme = signal<ResolvedTheme>(resolveTheme(readStoredTheme()));

  constructor() {
    // Apply the resolved theme whenever the preference changes.
    effect(() => {
      const resolved = resolveTheme(this.theme());
      document.documentElement.classList.toggle('dark', resolved === 'dark');
      this.resolvedTheme.set(resolved);
    });

    // While following the OS, react to live ``prefers-color-scheme`` changes.
    const media = window.matchMedia(DARK_QUERY);
    media.addEventListener('change', () => {
      if (this.theme() !== 'system') return;
      const resolved: ResolvedTheme = media.matches ? 'dark' : 'light';
      document.documentElement.classList.toggle('dark', resolved === 'dark');
      this.resolvedTheme.set(resolved);
    });
  }

  /** Update (and persist) the theme preference. */
  setTheme(next: Theme): void {
    try {
      if (next === 'system') {
        localStorage.removeItem(STORAGE_KEY);
      } else {
        localStorage.setItem(STORAGE_KEY, next);
      }
    } catch {
      /* Persistence is best-effort; theme still applies for this session. */
    }
    this.theme.set(next);
  }
}
