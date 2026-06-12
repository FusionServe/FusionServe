import { useCallback } from "react";

import { useAuth } from "./auth";
import { useRuntimeConfig } from "./runtimeConfig";

/** A GraphQL error returned in the ``errors`` array of a response. */
export interface GraphQLError {
  message: string;
  path?: (string | number)[];
  extensions?: Record<string, unknown>;
}

/** Error thrown when a GraphQL response carries an ``errors`` array. */
export class GraphQLRequestError extends Error {
  readonly errors: GraphQLError[];
  constructor(errors: GraphQLError[]) {
    super(errors.map((e) => e.message).join("; ") || "GraphQL request failed");
    this.name = "GraphQLRequestError";
    this.errors = errors;
  }
}

interface GraphQLResponse<T> {
  data?: T;
  errors?: GraphQLError[];
}

/**
 * Execute a GraphQL operation against the backend.
 *
 * Sends a POST to ``url`` (the runtime-configured GraphQL endpoint, proxied
 * to the Litestar backend in dev). When ``token`` is provided it is sent as
 * a Bearer credential so PostgreSQL row-level security applies the
 * authenticated role; otherwise the request runs as the anonymous role.
 *
 * @throws {GraphQLRequestError} if the response contains a non-empty
 *   ``errors`` array.
 * @throws {Error} on transport/HTTP failures.
 */
export async function gqlRequest<T>(
  url: string,
  query: string,
  variables?: Record<string, unknown>,
  token?: string | null,
): Promise<T> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    Accept: "application/json",
  };
  if (token) {
    headers.Authorization = `Bearer ${token}`;
  }
  const resp = await fetch(url, {
    method: "POST",
    headers,
    body: JSON.stringify({ query, variables }),
  });
  if (!resp.ok) {
    throw new Error(`GraphQL HTTP ${resp.status} ${resp.statusText}`);
  }
  const body = (await resp.json()) as GraphQLResponse<T>;
  if (body.errors && body.errors.length > 0) {
    throw new GraphQLRequestError(body.errors);
  }
  if (body.data === undefined) {
    throw new Error("GraphQL response had no data");
  }
  return body.data;
}

/**
 * Hook returning a {@link gqlRequest} bound to the runtime GraphQL URL and
 * the current access token.
 *
 * The returned function lazily resolves the runtime config on first call
 * (so it works without a config fetch until a query actually runs) and reads
 * the latest token via the auth context's ``getAccessToken`` getter, so
 * silent token renewal is picked up transparently.
 */
export function useGql(): <T>(
  query: string,
  variables?: Record<string, unknown>,
) => Promise<T> {
  const { getAccessToken } = useAuth();
  const { ensureConfig } = useRuntimeConfig();
  return useCallback(
    async <T>(query: string, variables?: Record<string, unknown>) => {
      const cfg = await ensureConfig();
      return gqlRequest<T>(cfg.graphqlUrl, query, variables, getAccessToken());
    },
    [ensureConfig, getAccessToken],
  );
}
