import {
  type ReactNode,
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  UserManager,
  type UserManagerSettings,
  type User as OidcUser,
  WebStorageStateStore,
} from "oidc-client-ts";

import type { RuntimeConfig } from "./api";
import { useRuntimeConfig } from "./runtimeConfig";

/**
 * OpenID Connect (Authorization Code + PKCE) authentication.
 *
 * The flow is deliberately *not* started on app load: the SPA boots as
 * "Anonymous" and only begins a login redirect when the user clicks the
 * badge ({@link AuthContextValue.login}). On load we merely (a) complete a
 * redirect callback if the IdP just sent us back, or (b) silently restore a
 * still-valid stored session — neither of which initiates authentication.
 *
 * Configuration (issuer + public client id) comes from the lazily-loaded
 * runtime config ({@link useRuntimeConfig}); the backend itself only serves a
 * subset of settings. To avoid a backend call on pages that don't need it,
 * config is fetched at first need: on mount only when there's a login
 * callback to complete or a stored session to restore, and otherwise on the
 * first login click. Endpoint discovery is delegated to oidc-client-ts,
 * which fetches ``<issuer>/.well-known/openid-configuration``.
 */

/** Coarse authentication state, surfaced to the badge UI. */
export type AuthStatus =
  | "anonymous"
  | "authenticating"
  | "authenticated"
  | "error";

/** Display-oriented view of the authenticated user. */
export interface AuthUser {
  displayName: string;
  username: string | null;
  email: string | null;
  /** 1–2 character avatar initials. */
  initials: string;
}

export interface AuthContextValue {
  status: AuthStatus;
  user: AuthUser | null;
  error: string | null;
  /** Whether the backend advertised a usable OIDC configuration. */
  configured: boolean;
  /** Begin the Authorization Code redirect (user-initiated only). */
  login: () => void;
  /** RP-initiated logout: clears tokens and ends the IdP session. */
  logout: () => void;
  /** Copy the current access token to the clipboard. Returns success. */
  copyAccessToken: () => Promise<boolean>;
  /** Current access token, or null when anonymous. For authed API calls. */
  getAccessToken: () => string | null;
}

const AuthContext = createContext<AuthContextValue | null>(null);

/**
 * One-shot, module-scoped cache of the redirect code→token exchange.
 *
 * React 19 StrictMode mounts effects twice in development, which would call
 * ``signinRedirectCallback()`` twice and fail the second time (the
 * single-use authorization ``code`` is already consumed). Caching the
 * promise ensures the exchange runs exactly once; both effect runs await it
 * and the live (non-cancelled) one applies the resulting user.
 */
let redirectExchange: Promise<OidcUser> | null = null;

function str(value: unknown): string | undefined {
  return typeof value === "string" && value.length > 0 ? value : undefined;
}

/** Derive avatar initials from the most specific claims available. */
function deriveInitials(
  given: string | undefined,
  family: string | undefined,
  fallback: string,
): string {
  if (given || family) {
    return `${given?.[0] ?? ""}${family?.[0] ?? ""}`.toUpperCase();
  }
  const words = fallback.trim().split(/\s+/);
  if (words.length >= 2) {
    return `${words[0][0]}${words[1][0]}`.toUpperCase();
  }
  return fallback.slice(0, 2).toUpperCase();
}

/** Map verified OIDC profile claims to the display-oriented {@link AuthUser}. */
function toAuthUser(user: OidcUser): AuthUser {
  const p = user.profile as Record<string, unknown>;
  const name = str(p.name);
  const preferred = str(p.preferred_username);
  const email = str(p.email);
  const given = str(p.given_name);
  const family = str(p.family_name);
  const displayName = name ?? preferred ?? email ?? "User";
  return {
    displayName,
    username: preferred ?? null,
    email: email ?? null,
    initials: deriveInitials(given, family, displayName) || "U",
  };
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const { ensureConfig } = useRuntimeConfig();
  const managerRef = useRef<UserManager | null>(null);
  const accessTokenRef = useRef<string | null>(null);
  const [status, setStatus] = useState<AuthStatus>("anonymous");
  const [user, setUser] = useState<AuthUser | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [configured, setConfigured] = useState(false);

  const applyUser = useCallback((u: OidcUser) => {
    accessTokenRef.current = u.access_token ?? null;
    setUser(toAuthUser(u));
    setError(null);
    setStatus("authenticated");
  }, []);

  const clearUser = useCallback(() => {
    accessTokenRef.current = null;
    setUser(null);
    setStatus("anonymous");
  }, []);

  // Build (once) the UserManager from resolved config. Returns null when the
  // backend has no OIDC configuration (no issuer / client id).
  const buildManager = useCallback(
    (cfg: RuntimeConfig): UserManager | null => {
      if (managerRef.current) return managerRef.current;
      if (!cfg.jwtIssuer || !cfg.clientId) return null;
      // Stable static mount of the SPA (the hash route lives in the fragment,
      // which the IdP preserves separately). Must be a registered redirect
      // URI on the IdP client.
      const appUrl = window.location.origin + window.location.pathname;
      const settings: UserManagerSettings = {
        authority: cfg.jwtIssuer,
        client_id: cfg.clientId,
        redirect_uri: appUrl,
        post_logout_redirect_uri: appUrl,
        response_type: "code",
        scope: "openid profile email",
        // Keep the access token valid in the background via the
        // refresh-token grant (no redirect; only while already logged in).
        automaticSilentRenew: true,
        // sessionStorage: restores on reload, cleared when the tab closes.
        userStore: new WebStorageStateStore({ store: window.sessionStorage }),
        stateStore: new WebStorageStateStore({ store: window.sessionStorage }),
      };
      const manager = new UserManager(settings);
      // Renewal / external updates keep the token + badge in sync.
      manager.events.addUserLoaded((u) => applyUser(u));
      manager.events.addUserUnloaded(() => clearUser());
      managerRef.current = manager;
      setConfigured(true);
      return manager;
    },
    [applyUser, clearUser],
  );

  // On mount, only touch the backend when there's a login callback to
  // complete or a stored session to restore. Pages that never authenticate
  // (e.g. Overview) make no request.
  useEffect(() => {
    let cancelled = false;
    const query = new URLSearchParams(window.location.search);
    const hasCallback = query.has("code") && query.has("state");
    const hasStoredSession = (() => {
      try {
        for (let i = 0; i < window.sessionStorage.length; i += 1) {
          if (window.sessionStorage.key(i)?.startsWith("oidc.user:")) return true;
        }
      } catch {
        /* sessionStorage unavailable */
      }
      return false;
    })();
    if (!hasCallback && !hasStoredSession) return;

    async function bootstrap() {
      const cfg = await ensureConfig();
      if (cancelled) return;
      const manager = buildManager(cfg);
      if (!manager) return;
      const appUrl = window.location.origin + window.location.pathname;

      // 1. Complete a redirect callback if the IdP just returned to us.
      if (hasCallback) {
        setStatus("authenticating");
        try {
          if (!redirectExchange) {
            redirectExchange = manager.signinRedirectCallback();
          }
          const u = await redirectExchange;
          if (cancelled) return;
          applyUser(u);
          const hash = typeof u.state === "string" ? u.state : window.location.hash;
          window.history.replaceState(null, "", appUrl + (hash || ""));
        } catch (e) {
          if (!cancelled) {
            setStatus("error");
            setError(e instanceof Error ? e.message : String(e));
          }
          // Drop the (now-consumed) code so a reload doesn't retry it.
          window.history.replaceState(null, "", appUrl + window.location.hash);
        }
        return;
      }

      // 2. Silently restore an existing session — no redirect, no auto-login.
      try {
        const existing = await manager.getUser();
        if (cancelled) return;
        if (existing && !existing.expired) {
          applyUser(existing);
        } else if (existing?.refresh_token) {
          const refreshed = await manager.signinSilent();
          if (cancelled) return;
          if (refreshed) applyUser(refreshed);
        }
      } catch {
        // A failed silent restore just leaves the user anonymous.
        if (!cancelled) clearUser();
      }
    }

    void bootstrap();
    return () => {
      cancelled = true;
    };
  }, [ensureConfig, buildManager, applyUser, clearUser]);

  const login = useCallback(() => {
    setStatus("authenticating");
    setError(null);
    // Lazily load config on first login, then build the manager and redirect.
    ensureConfig()
      .then((cfg) => {
        const manager = buildManager(cfg);
        if (!manager) {
          setStatus("error");
          setError("Authentication is not configured on this server.");
          return;
        }
        // Preserve the current SPA (hash) route so we can return to it.
        return manager.signinRedirect({ state: window.location.hash });
      })
      .catch((e) => {
        setStatus("error");
        setError(e instanceof Error ? e.message : String(e));
      });
  }, [ensureConfig, buildManager]);

  const logout = useCallback(() => {
    const manager = managerRef.current;
    if (!manager) {
      clearUser();
      return;
    }
    manager.signoutRedirect().catch(() => {
      // If end-session fails, at least clear the local session.
      void manager.removeUser().finally(clearUser);
    });
  }, [clearUser]);

  const copyAccessToken = useCallback(async () => {
    const token = accessTokenRef.current;
    if (!token) return false;
    try {
      await navigator.clipboard.writeText(token);
      return true;
    } catch {
      return false;
    }
  }, []);

  const getAccessToken = useCallback(() => accessTokenRef.current, []);

  const value = useMemo<AuthContextValue>(
    () => ({
      status,
      user,
      error,
      configured,
      login,
      logout,
      copyAccessToken,
      getAccessToken,
    }),
    [
      status,
      user,
      error,
      configured,
      login,
      logout,
      copyAccessToken,
      getAccessToken,
    ],
  );

  return <AuthContext value={value}>{children}</AuthContext>;
}

/** Access the authentication context. Must be used within {@link AuthProvider}. */
export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (ctx === null) {
    throw new Error("useAuth must be used within an AuthProvider");
  }
  return ctx;
}
