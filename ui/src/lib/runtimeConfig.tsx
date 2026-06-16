import {
  type ReactNode,
  createContext,
  useCallback,
  useContext,
  useMemo,
  useState,
} from "react";

import { type RuntimeConfig, fetchRuntimeConfig } from "./api";

/**
 * Lazy runtime-configuration context.
 *
 * Nothing is fetched on mount: pages that don't talk to the backend (e.g.
 * the Overview page) incur no network call. Consumers call
 * {@link RuntimeConfigContextValue.ensureConfig} at first need (a GraphQL
 * request, the OpenAPI/GraphQL viewers, a login, or a session restore); the
 * underlying fetch is memoized so it happens at most once.
 */
interface RuntimeConfigContextValue {
  /** Resolved config once loaded, else ``null``. */
  config: RuntimeConfig | null;
  /** Load (or return the in-flight/cached) configuration. */
  ensureConfig: () => Promise<RuntimeConfig>;
}

const RuntimeConfigContext = createContext<RuntimeConfigContextValue | null>(
  null,
);

export function RuntimeConfigProvider({ children }: { children: ReactNode }) {
  const [config, setConfig] = useState<RuntimeConfig | null>(null);

  const ensureConfig = useCallback(async () => {
    const resolved = await fetchRuntimeConfig();
    // ``fetchRuntimeConfig`` is module-memoized; updating state on every call
    // is cheap and keeps ``config`` populated after the first resolution.
    setConfig(resolved);
    return resolved;
  }, []);

  const value = useMemo<RuntimeConfigContextValue>(
    () => ({ config, ensureConfig }),
    [config, ensureConfig],
  );

  return (
    <RuntimeConfigContext value={value}>{children}</RuntimeConfigContext>
  );
}

/** Access the lazy runtime-configuration context. */
export function useRuntimeConfig(): RuntimeConfigContextValue {
  const ctx = useContext(RuntimeConfigContext);
  if (ctx === null) {
    throw new Error("useRuntimeConfig must be used within a RuntimeConfigProvider");
  }
  return ctx;
}
