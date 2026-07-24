import { APP_BASE_HREF } from '@angular/common';
import { Injectable, inject, signal } from '@angular/core';
import { Router } from '@angular/router';
import {
  type User as OidcUser,
  UserManager,
  type UserManagerSettings,
  WebStorageStateStore,
} from 'oidc-client-ts';

import { ConfigService, type RuntimeConfig } from './config.service';

/**
 * OpenID Connect (Authorization Code + PKCE) authentication.
 *
 * The flow is deliberately *not* started on app load: the SPA boots as
 * "Anonymous" and only begins a login redirect when the user clicks the badge
 * ({@link AuthService.login}). On load ({@link AuthService.bootstrap}) it
 * merely (a) completes a redirect callback if the IdP just sent us back, or
 * (b) silently restores a still-valid stored session — neither of which
 * initiates authentication. The IdP returns to the SPA mount root (a stable
 * ``redirect_uri``); the route the user was on is preserved via OIDC ``state``
 * and restored with a router navigation after the token exchange.
 *
 * Configuration (issuer + public client id) comes from the lazily-loaded
 * runtime config ({@link ConfigService}); endpoint discovery is delegated to
 * oidc-client-ts, which fetches ``<issuer>/.well-known/openid-configuration``.
 */

/** Coarse authentication state, surfaced to the badge UI. */
export type AuthStatus = 'anonymous' | 'authenticating' | 'authenticated' | 'error';

/** Display-oriented view of the authenticated user. */
export interface AuthUser {
  displayName: string;
  username: string | null;
  email: string | null;
  /** 1–2 character avatar initials. */
  initials: string;
}

function str(value: unknown): string | undefined {
  return typeof value === 'string' && value.length > 0 ? value : undefined;
}

/** Derive avatar initials from the most specific claims available. */
function deriveInitials(
  given: string | undefined,
  family: string | undefined,
  fallback: string,
): string {
  if (given || family) {
    return `${given?.[0] ?? ''}${family?.[0] ?? ''}`.toUpperCase();
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
  const name = str(p['name']);
  const preferred = str(p['preferred_username']);
  const email = str(p['email']);
  const given = str(p['given_name']);
  const family = str(p['family_name']);
  const displayName = name ?? preferred ?? email ?? 'User';
  return {
    displayName,
    username: preferred ?? null,
    email: email ?? null,
    initials: deriveInitials(given, family, displayName) || 'U',
  };
}

@Injectable({ providedIn: 'root' })
export class AuthService {
  private readonly configService = inject(ConfigService);
  private readonly router = inject(Router);
  private readonly baseHref = inject(APP_BASE_HREF);

  /** Coarse authentication state. */
  readonly status = signal<AuthStatus>('anonymous');
  /** Display view of the current user, or ``null`` when anonymous. */
  readonly user = signal<AuthUser | null>(null);
  /** Human-readable error message from the last failed auth attempt. */
  readonly error = signal<string | null>(null);
  /** Whether the backend advertised a usable OIDC configuration. */
  readonly configured = signal(false);

  private manager: UserManager | null = null;
  private accessToken: string | null = null;
  /**
   * One-shot cache of the redirect code→token exchange. Guards against a
   * double bootstrap consuming the single-use authorization ``code`` twice.
   */
  private redirectExchange: Promise<OidcUser> | null = null;

  private applyUser(u: OidcUser): void {
    this.accessToken = u.access_token ?? null;
    this.user.set(toAuthUser(u));
    this.error.set(null);
    this.status.set('authenticated');
  }

  private clearUser(): void {
    this.accessToken = null;
    this.user.set(null);
    this.status.set('anonymous');
  }

  /**
   * Absolute mount URL of the SPA (origin + router base).
   *
   * Used as the OIDC ``redirect_uri`` / ``post_logout_redirect_uri``: a single
   * stable value (the SPA root), independent of the current deep route.
   */
  private mountUrl(): string {
    const base = this.baseHref.endsWith('/') ? this.baseHref : `${this.baseHref}/`;
    return new URL(base, window.location.origin).href;
  }

  /** The current in-app route (path relative to the router base). */
  private currentRoute(): string {
    return this.router.url || '/';
  }

  /**
   * Build (once) the UserManager from resolved config. Returns ``null`` when
   * the backend has no OIDC configuration (no issuer / client id).
   */
  private buildManager(cfg: RuntimeConfig): UserManager | null {
    if (this.manager) return this.manager;
    if (!cfg.jwtIssuer || !cfg.clientId) return null;
    const appUrl = this.mountUrl();
    const settings: UserManagerSettings = {
      authority: cfg.jwtIssuer,
      client_id: cfg.clientId,
      redirect_uri: appUrl,
      post_logout_redirect_uri: appUrl,
      response_type: 'code',
      scope: 'openid profile email',
      // Keep the access token valid in the background via the refresh-token
      // grant (no redirect; only while already logged in).
      automaticSilentRenew: true,
      // sessionStorage: restores on reload, cleared when the tab closes.
      userStore: new WebStorageStateStore({ store: window.sessionStorage }),
      stateStore: new WebStorageStateStore({ store: window.sessionStorage }),
    };
    const manager = new UserManager(settings);
    manager.events.addUserLoaded((u) => this.applyUser(u));
    manager.events.addUserUnloaded(() => this.clearUser());
    this.manager = manager;
    this.configured.set(true);
    return manager;
  }

  /**
   * On app load, only touch the backend when there's a login callback to
   * complete or a stored session to restore. Pages that never authenticate
   * (e.g. Overview) make no request.
   */
  async bootstrap(): Promise<void> {
    const query = new URLSearchParams(window.location.search);
    const hasCallback = query.has('code') && query.has('state');
    const hasStoredSession = (() => {
      try {
        for (let i = 0; i < window.sessionStorage.length; i += 1) {
          if (window.sessionStorage.key(i)?.startsWith('oidc.user:')) return true;
        }
      } catch {
        /* sessionStorage unavailable */
      }
      return false;
    })();
    if (!hasCallback && !hasStoredSession) return;

    const cfg = await this.configService.ensureConfig();
    const manager = this.buildManager(cfg);
    if (!manager) return;

    // 1. Complete a redirect callback if the IdP just returned to us.
    if (hasCallback) {
      this.status.set('authenticating');
      try {
        if (!this.redirectExchange) {
          this.redirectExchange = manager.signinRedirectCallback();
        }
        const u = await this.redirectExchange;
        this.applyUser(u);
        // Restore the pre-login route (saved in OIDC state); this also drops
        // the now-consumed ``?code&state`` query from the URL.
        const to = typeof u.state === 'string' && u.state ? u.state : '/';
        void this.router.navigateByUrl(to, { replaceUrl: true });
      } catch (e) {
        this.status.set('error');
        this.error.set(e instanceof Error ? e.message : String(e));
        void this.router.navigateByUrl('/', { replaceUrl: true });
      }
      return;
    }

    // 2. Silently restore an existing session — no redirect, no auto-login.
    try {
      const existing = await manager.getUser();
      if (existing && !existing.expired) {
        this.applyUser(existing);
      } else if (existing?.refresh_token) {
        const refreshed = await manager.signinSilent();
        if (refreshed) this.applyUser(refreshed);
      }
    } catch {
      // A failed silent restore just leaves the user anonymous.
      this.clearUser();
    }
  }

  /** Begin the Authorization Code redirect (user-initiated only). */
  login(): void {
    this.status.set('authenticating');
    this.error.set(null);
    this.configService
      .ensureConfig()
      .then((cfg) => {
        const manager = this.buildManager(cfg);
        if (!manager) {
          this.status.set('error');
          this.error.set('Authentication is not configured on this server.');
          return;
        }
        // Preserve the current SPA route so we can return to it after login.
        return manager.signinRedirect({ state: this.currentRoute() });
      })
      .catch((e) => {
        this.status.set('error');
        this.error.set(e instanceof Error ? e.message : String(e));
      });
  }

  /** RP-initiated logout: clears tokens and ends the IdP session. */
  logout(): void {
    const manager = this.manager;
    if (!manager) {
      this.clearUser();
      return;
    }
    manager.signoutRedirect().catch(() => {
      void manager.removeUser().finally(() => this.clearUser());
    });
  }

  /** Copy the current access token to the clipboard. Returns success. */
  async copyAccessToken(): Promise<boolean> {
    if (!this.accessToken) return false;
    try {
      await navigator.clipboard.writeText(this.accessToken);
      return true;
    } catch {
      return false;
    }
  }

  /** Current access token, or ``null`` when anonymous. For authed API calls. */
  getAccessToken(): string | null {
    return this.accessToken;
  }
}
