import { useEffect } from "react";
import SwaggerUI from "swagger-ui-react";

// Swagger UI is now bundled directly (replacing the previous iframe to the
// backend-served Swagger page). It fetches the raw spec from the
// runtime-configured ``openapi.json`` URL; in dev Vite proxies that to the
// Litestar backend, and "Try it out" requests hit the REST endpoints through
// the same proxy.
import "swagger-ui-react/swagger-ui.css";
// Best-effort dark theme. Swagger UI has no theming API, so this file
// hand-overrides its surfaces under the app's ``.dark`` class. Co-located
// with this lazily-imported page so it stays out of the global bundle.
import "@/styles/swagger-dark.css";

import { useRuntimeConfig } from "@/lib/runtimeConfig";

export function OpenAPIPage() {
  const { config, ensureConfig } = useRuntimeConfig();

  // Lazily resolve the backend config (and thus the OpenAPI URL) on mount.
  useEffect(() => {
    void ensureConfig();
  }, [ensureConfig]);

  return (
    <div className="h-[calc(100vh-9rem)] overflow-auto rounded-lg border border-zinc-200 bg-white dark:border-zinc-800 dark:bg-zinc-950">
      {config ? (
        <SwaggerUI url={config.openapiUrl} />
      ) : (
        <div className="flex h-full items-center justify-center text-sm text-zinc-500 dark:text-zinc-400">
          Loading…
        </div>
      )}
    </div>
  );
}
