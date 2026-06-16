// Runtime configuration utilities.
//
// The backend exposes a client-configuration document at
// ``/.well-known/configuration`` (also served at ``<base_path>/config.json``).
// The SPA fetches it lazily — only when something first needs the backend —
// and derives every REST/GraphQL endpoint from the returned ``base_path``,
// so the UI never hardcodes URLs and follows a relocated backend.

/** Base-path-independent client configuration endpoint. */
export const WELLKNOWN_CONFIG_URL = "/.well-known/configuration";

/** Fallback base path used when the backend can't be reached. */
export const DEFAULT_BASE_PATH = "/api";

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
  const basePath = (raw.base_path || DEFAULT_BASE_PATH).replace(/\/+$/, "");
  return {
    basePath,
    graphqlUrl: `${basePath}/graphql`,
    openapiUrl: `${basePath}/openapi.json`,
    swaggerUrl: `${basePath}/swagger`,
    jwtIssuer: raw.jwt_issuer ?? null,
    clientId: raw.client_id ?? null,
  };
}

let configPromise: Promise<RuntimeConfig> | null = null;

/**
 * Fetch and cache the runtime configuration.
 *
 * Memoized for the page lifetime: concurrent and repeat callers share one
 * network request. If the endpoint is unreachable or returns an error, the
 * promise resolves to a fallback derived from {@link DEFAULT_BASE_PATH} so
 * the app keeps working (auth simply stays unavailable).
 */
export function fetchRuntimeConfig(): Promise<RuntimeConfig> {
  if (!configPromise) {
    configPromise = (async () => {
      try {
        const resp = await fetch(WELLKNOWN_CONFIG_URL, {
          headers: { Accept: "application/json" },
        });
        if (!resp.ok) {
          throw new Error(`configuration endpoint responded ${resp.status}`);
        }
        return deriveConfig((await resp.json()) as RawConfig);
      } catch {
        return deriveConfig({});
      }
    })();
  }
  return configPromise;
}
