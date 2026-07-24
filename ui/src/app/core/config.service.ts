import { Injectable, signal } from '@angular/core';

/**
 * Runtime configuration service.
 *
 * The backend exposes a client-configuration document at
 * ``/.well-known/configuration`` (also served at ``<base_path>/config.json``).
 * The SPA fetches it lazily — only when something first needs the backend —
 * and derives every REST/GraphQL endpoint from the returned ``base_path``, so
 * the UI never hardcodes URLs and follows a relocated backend.
 */

/** Base-path-independent client configuration endpoint. */
export const WELLKNOWN_CONFIG_URL = '/.well-known/configuration';

/** Fallback base path used when the backend can't be reached. */
export const DEFAULT_BASE_PATH = '/api';

/** Raw payload returned by the configuration endpoint (snake_case). */
interface RawConfig {
  base_path?: string | null;
  jwt_issuer?: string | null;
  jwks_url?: string | null;
  client_id?: string | null;
}

/** Resolved runtime configuration with derived endpoint URLs. */
export interface RuntimeConfig {
  /** Backend API base path (e.g. ``/api``). */
  basePath: string;
  /** GraphQL endpoint URL. */
  graphqlUrl: string;
  /** Raw OpenAPI 3.1 document URL. */
  openapiUrl: string;
  /** Backend-served Swagger UI URL. */
  swaggerUrl: string;
  /** OIDC issuer, when authentication is configured. */
  jwtIssuer: string | null;
  /** Public OIDC client id, when authentication is configured. */
  clientId: string | null;
}

/** Compose the runtime config (derived endpoints) from a raw payload. */
export function deriveConfig(raw: RawConfig): RuntimeConfig {
  const basePath = (raw.base_path || DEFAULT_BASE_PATH).replace(/\/+$/, '');
  return {
    basePath,
    graphqlUrl: `${basePath}/graphql`,
    openapiUrl: `${basePath}/openapi.json`,
    swaggerUrl: `${basePath}/swagger`,
    jwtIssuer: raw.jwt_issuer ?? null,
    clientId: raw.client_id ?? null,
  };
}

/**
 * Lazy runtime-configuration service.
 *
 * Nothing is fetched on construction: pages that don't talk to the backend
 * (e.g. the Overview page) incur no network call. Consumers call
 * {@link ConfigService.ensureConfig} at first need (a GraphQL request, the
 * OpenAPI/GraphQL viewers, a login, or a session restore); the underlying
 * fetch is memoized so it happens at most once. If the endpoint is unreachable
 * or errors, the promise resolves to a fallback derived from
 * {@link DEFAULT_BASE_PATH} so the app keeps working (auth simply stays
 * unavailable).
 */
@Injectable({ providedIn: 'root' })
export class ConfigService {
  /** Resolved config once loaded, else ``null``. */
  readonly config = signal<RuntimeConfig | null>(null);

  private configPromise: Promise<RuntimeConfig> | null = null;

  /** Load (or return the in-flight/cached) configuration. */
  ensureConfig(): Promise<RuntimeConfig> {
    if (!this.configPromise) {
      this.configPromise = (async () => {
        let resolved: RuntimeConfig;
        try {
          const resp = await fetch(WELLKNOWN_CONFIG_URL, {
            headers: { Accept: 'application/json' },
          });
          if (!resp.ok) {
            throw new Error(`configuration endpoint responded ${resp.status}`);
          }
          resolved = deriveConfig((await resp.json()) as RawConfig);
        } catch {
          resolved = deriveConfig({});
        }
        this.config.set(resolved);
        return resolved;
      })();
    }
    return this.configPromise;
  }
}
