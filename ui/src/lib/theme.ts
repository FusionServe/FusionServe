import { useCallback, useEffect, useState } from "react";

/**
 * User-selectable theme preference.
 *
 * ``"system"`` defers to the OS ``prefers-color-scheme`` and is the
 * default when nothing is persisted.
 */
export type Theme = "light" | "dark" | "system";

/** Concrete theme actually applied to the document. */
export type ResolvedTheme = "light" | "dark";

const STORAGE_KEY = "theme";
const DARK_QUERY = "(prefers-color-scheme: dark)";

function prefersDark(): boolean {
  return (
    typeof window !== "undefined" &&
    window.matchMedia(DARK_QUERY).matches
  );
}

/** Read the persisted preference, defaulting to ``"system"``. */
function readStoredTheme(): Theme {
  try {
    const value = localStorage.getItem(STORAGE_KEY);
    if (value === "light" || value === "dark") {
      return value;
    }
  } catch {
    /* localStorage unavailable (private mode); fall through to default. */
  }
  return "system";
}

/** Resolve a preference to the concrete light/dark theme to apply. */
function resolveTheme(theme: Theme): ResolvedTheme {
  if (theme === "system") {
    return prefersDark() ? "dark" : "light";
  }
  return theme;
}

/** Toggle the ``.dark`` class on ``<html>`` to match the resolved theme. */
function applyResolvedTheme(resolved: ResolvedTheme): void {
  document.documentElement.classList.toggle("dark", resolved === "dark");
}

/**
 * Manage the application theme.
 *
 * Persists the user's explicit ``"light"``/``"dark"`` choice in
 * ``localStorage`` and removes it when they return to ``"system"`` so the
 * app follows the OS again. Keeps the document ``.dark`` class in sync and
 * reacts to OS changes while in ``"system"`` mode.
 *
 * Returns:
 *     The current preference, the resolved (applied) theme, and a setter.
 */
export function useTheme(): {
  theme: Theme;
  resolvedTheme: ResolvedTheme;
  setTheme: (theme: Theme) => void;
} {
  const [theme, setThemeState] = useState<Theme>(readStoredTheme);
  const [resolvedTheme, setResolvedTheme] = useState<ResolvedTheme>(() =>
    resolveTheme(readStoredTheme()),
  );

  // Apply the resolved theme whenever the preference changes.
  useEffect(() => {
    const resolved = resolveTheme(theme);
    applyResolvedTheme(resolved);
    setResolvedTheme(resolved);
  }, [theme]);

  // While following the OS, react to live ``prefers-color-scheme`` changes.
  useEffect(() => {
    if (theme !== "system") {
      return;
    }
    const media = window.matchMedia(DARK_QUERY);
    const onChange = () => {
      const resolved: ResolvedTheme = media.matches ? "dark" : "light";
      applyResolvedTheme(resolved);
      setResolvedTheme(resolved);
    };
    media.addEventListener("change", onChange);
    return () => media.removeEventListener("change", onChange);
  }, [theme]);

  const setTheme = useCallback((next: Theme) => {
    try {
      if (next === "system") {
        localStorage.removeItem(STORAGE_KEY);
      } else {
        localStorage.setItem(STORAGE_KEY, next);
      }
    } catch {
      /* Persistence is best-effort; theme still applies for this session. */
    }
    setThemeState(next);
  }, []);

  return { theme, resolvedTheme, setTheme };
}
