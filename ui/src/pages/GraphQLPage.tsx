import { useMemo } from "react";
import { GraphiQL, HISTORY_PLUGIN } from "graphiql";
import { createGraphiQLFetcher } from "@graphiql/toolkit";
import { explorerPlugin } from "@graphiql/plugin-explorer";

// GraphiQL 5 renders its editors with Monaco, which needs web workers
// wired up at the bundler level. This side-effect import installs
// ``MonacoEnvironment.getWorker`` from our own source (see
// ``@/lib/monaco-workers`` for why we don't use
// ``graphiql/setup-workers/vite`` directly under pnpm). It lives here
// rather than in ``main.tsx`` so Monaco only loads inside this
// lazily-imported chunk.
import "@/lib/monaco-workers";
import "graphiql/style.css";
import "@graphiql/plugin-explorer/style.css";

import { GRAPHQL_URL } from "@/lib/api";
import { useTheme } from "@/lib/theme";

// GraphiQL is now bundled directly (replacing the previous iframe to the
// backend-served IDE). Queries go to the same ``/api/graphql`` endpoint
// over POST; in dev Vite proxies that to the Litestar backend. The
// backend still serves its own GraphiQL on GET as a fallback, but the
// SPA no longer depends on it.
export function GraphQLPage() {
  const { resolvedTheme } = useTheme();
  const fetcher = useMemo(
    () => createGraphiQLFetcher({ url: GRAPHQL_URL }),
    [],
  );
  // Visual query-builder panel, matching the explorer that Strawberry's
  // backend-served GraphiQL shipped. Stable instance across renders.
  const explorer = useMemo(() => explorerPlugin(), []);

  return (
    <div className="h-[calc(100vh-9rem)] overflow-hidden rounded-lg border border-zinc-200 dark:border-zinc-800">
      {/* ``forcedTheme`` keeps GraphiQL in lockstep with the app-wide
          theme toggle instead of GraphiQL's own persisted setting.
          Passing ``plugins`` *replaces* GraphiQL's default ``[HISTORY_PLUGIN]``
          (the doc-explorer is a separate always-on referencePlugin), so we
          re-add ``HISTORY_PLUGIN`` here alongside the explorer. Its styles
          ship in ``graphiql/style.css``. */}
      <GraphiQL
        fetcher={fetcher}
        forcedTheme={resolvedTheme}
        plugins={[HISTORY_PLUGIN, explorer]}
      />
    </div>
  );
}
