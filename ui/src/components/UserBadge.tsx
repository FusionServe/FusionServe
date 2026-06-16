import { useEffect, useRef, useState } from "react";

import { useAuth } from "@/lib/auth";

/**
 * Header user badge (see ``docs/dashboard-screenshot.png``).
 *
 * - Anonymous: a neutral pill reading "Anonymous"; clicking starts the
 *   OIDC login redirect.
 * - Authenticating: a disabled pill with a spinner.
 * - Authenticated: an avatar (initials) + display name + chevron; clicking
 *   opens a menu with the user's email, "Copy access token" and "Sign out".
 */
export function UserBadge() {
  const { status, user, error, login, logout, copyAccessToken } = useAuth();
  const [open, setOpen] = useState(false);
  const [copied, setCopied] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);

  // Close the menu on outside click or Escape.
  useEffect(() => {
    if (!open) return;
    function onPointerDown(event: MouseEvent) {
      if (!containerRef.current?.contains(event.target as Node)) {
        setOpen(false);
      }
    }
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") setOpen(false);
    }
    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);

  // Authenticating: non-interactive pill with spinner.
  if (status === "authenticating") {
    return (
      <span className="inline-flex items-center gap-2 rounded-full border border-zinc-200 bg-white px-3 py-1.5 text-sm font-medium text-zinc-500 dark:border-zinc-700 dark:bg-zinc-800 dark:text-zinc-400">
        <Spinner />
        <span className="hidden sm:inline">Signing in…</span>
      </span>
    );
  }

  // Anonymous (or errored): click to log in. Login is enabled optimistically
  // — config is loaded on click; if the backend has no OIDC issuer, the auth
  // context surfaces an error which we show as the button tooltip.
  if (status !== "authenticated" || !user) {
    return (
      <button
        type="button"
        onClick={login}
        title={error ?? "Sign in"}
        aria-label="Sign in"
        className="inline-flex items-center gap-2 rounded-full border border-zinc-200 bg-white px-3 py-1.5 text-sm font-medium text-zinc-700 shadow-sm transition-colors hover:bg-zinc-50 hover:text-zinc-900 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 dark:border-zinc-700 dark:bg-zinc-800 dark:text-zinc-200 dark:hover:bg-zinc-700 dark:hover:text-zinc-50"
      >
        <span className="flex h-6 w-6 items-center justify-center rounded-full bg-zinc-200 text-zinc-500 dark:bg-zinc-700 dark:text-zinc-300">
          <UserIcon />
        </span>
        <span>Anonymous</span>
      </button>
    );
  }

  // Authenticated: avatar + name + menu.
  async function onCopyToken() {
    const ok = await copyAccessToken();
    setCopied(ok);
    if (ok) {
      window.setTimeout(() => setCopied(false), 1500);
    }
  }

  return (
    <div ref={containerRef} className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-haspopup="menu"
        aria-expanded={open}
        title={user.displayName}
        className="inline-flex items-center gap-2 rounded-full border border-zinc-200 bg-white py-1 pe-2 ps-1 text-sm font-medium text-zinc-800 shadow-sm transition-colors hover:bg-zinc-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 dark:border-zinc-700 dark:bg-zinc-800 dark:text-zinc-100 dark:hover:bg-zinc-700"
      >
        <span className="flex h-7 w-7 items-center justify-center rounded-full bg-blue-600 text-xs font-semibold text-white">
          {user.initials}
        </span>
        <span className="hidden max-w-[12rem] truncate sm:inline">
          {user.displayName}
        </span>
        <ChevronIcon className={open ? "rotate-180" : ""} />
      </button>

      {open && (
        <div
          role="menu"
          className="absolute end-0 mt-2 w-64 overflow-hidden rounded-lg border border-zinc-200 bg-white py-1 shadow-lg dark:border-zinc-700 dark:bg-zinc-800"
        >
          <div className="border-b border-zinc-100 px-3 py-2 dark:border-zinc-700">
            <p className="truncate text-sm font-semibold text-zinc-900 dark:text-zinc-50">
              {user.displayName}
            </p>
            {user.email && (
              <p className="truncate text-xs text-zinc-500 dark:text-zinc-400">
                {user.email}
              </p>
            )}
          </div>
          <button
            type="button"
            role="menuitem"
            onClick={onCopyToken}
            className="flex w-full items-center gap-2 px-3 py-2 text-left text-sm text-zinc-700 transition-colors hover:bg-zinc-100 dark:text-zinc-200 dark:hover:bg-zinc-700"
          >
            <CopyIcon />
            {copied ? "Copied!" : "Copy access token"}
          </button>
          <button
            type="button"
            role="menuitem"
            onClick={() => {
              setOpen(false);
              logout();
            }}
            className="flex w-full items-center gap-2 px-3 py-2 text-left text-sm text-zinc-700 transition-colors hover:bg-zinc-100 dark:text-zinc-200 dark:hover:bg-zinc-700"
          >
            <LogoutIcon />
            Sign out
          </button>
        </div>
      )}
    </div>
  );
}

function Spinner() {
  return (
    <svg
      className="h-4 w-4 animate-spin"
      viewBox="0 0 24 24"
      fill="none"
      aria-hidden="true"
    >
      <circle
        className="opacity-25"
        cx="12"
        cy="12"
        r="10"
        stroke="currentColor"
        strokeWidth="4"
      />
      <path
        className="opacity-75"
        fill="currentColor"
        d="M4 12a8 8 0 0 1 8-8V0C5.4 0 0 5.4 0 12h4z"
      />
    </svg>
  );
}

function UserIcon() {
  return (
    <svg
      className="h-4 w-4"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M19 21v-2a4 4 0 0 0-4-4H9a4 4 0 0 0-4 4v2" />
      <circle cx="12" cy="7" r="4" />
    </svg>
  );
}

function ChevronIcon({ className = "" }: { className?: string }) {
  return (
    <svg
      className={`h-4 w-4 text-zinc-400 transition-transform ${className}`}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="m6 9 6 6 6-6" />
    </svg>
  );
}

function CopyIcon() {
  return (
    <svg
      className="h-4 w-4"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <rect x="9" y="9" width="13" height="13" rx="2" ry="2" />
      <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
    </svg>
  );
}

function LogoutIcon() {
  return (
    <svg
      className="h-4 w-4"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" />
      <polyline points="16 17 21 12 16 7" />
      <line x1="21" y1="12" x2="9" y2="12" />
    </svg>
  );
}
