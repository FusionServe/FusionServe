import SwaggerUI from "swagger-ui-react";

// Swagger UI is now bundled directly (replacing the previous iframe to the
// backend-served ``/api/swagger``). It fetches the raw spec from
// ``/api/openapi.json``; in dev Vite proxies that to the Litestar backend,
// and "Try it out" requests hit ``/api/v1/...`` through the same proxy.
import "swagger-ui-react/swagger-ui.css";
// Best-effort dark theme. Swagger UI has no theming API, so this file
// hand-overrides its surfaces under the app's ``.dark`` class. Co-located
// with this lazily-imported page so it stays out of the global bundle.
import "@/styles/swagger-dark.css";

import { OPENAPI_URL } from "@/lib/api";

export function OpenAPIPage() {
  return (
    <div className="h-[calc(100vh-9rem)] overflow-auto rounded-lg border border-zinc-200 bg-white dark:border-zinc-800 dark:bg-zinc-950">
      <SwaggerUI url={OPENAPI_URL} />
    </div>
  );
}
