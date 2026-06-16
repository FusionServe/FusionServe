import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Link, Outlet } from "@tanstack/react-router";

import { useDataSchema } from "./useDataSchema";

// One QueryClient for the whole (lazily-loaded) data feature.
const queryClient = new QueryClient({
  defaultOptions: { queries: { refetchOnWindowFocus: false } },
});

/**
 * Layout for the data-editing feature: a left nav listing every database
 * table (discovered from the ``*Connection`` query fields) and an
 * ``<Outlet/>`` rendering the selected table's grid.
 */
export function DataLayout() {
  return (
    <QueryClientProvider client={queryClient}>
      <DataLayoutInner />
    </QueryClientProvider>
  );
}

function DataLayoutInner() {
  const { data, isLoading, error } = useDataSchema();

  return (
    <div className="flex h-full gap-4">
      <aside className="w-60 shrink-0 overflow-y-auto rounded-lg border border-zinc-200 bg-white p-2 dark:border-zinc-800 dark:bg-zinc-900">
        <p className="px-2 py-1.5 text-xs font-semibold uppercase tracking-wide text-zinc-400 dark:text-zinc-500">
          Tables
        </p>
        {isLoading && (
          <p className="px-2 py-1.5 text-sm text-zinc-500 dark:text-zinc-400">
            Loading schema…
          </p>
        )}
        {error && (
          <p className="px-2 py-1.5 text-sm text-red-600 dark:text-red-400">
            {error instanceof Error ? error.message : "Failed to load schema"}
          </p>
        )}
        <nav className="flex flex-col">
          {data?.tables.map((table) => (
            <Link
              key={table.name}
              to="/data/$table"
              params={{ table: table.name }}
              className="truncate rounded-md px-2 py-1.5 text-sm text-zinc-600 transition-colors hover:bg-zinc-100 hover:text-zinc-900 data-[active]:bg-blue-50 data-[active]:font-medium data-[active]:text-blue-700 dark:text-zinc-400 dark:hover:bg-zinc-800 dark:hover:text-zinc-100 dark:data-[active]:bg-blue-950 dark:data-[active]:text-blue-300"
              activeProps={{ "data-active": "true" }}
              title={table.label}
            >
              {table.label}
            </Link>
          ))}
          {data && data.tables.length === 0 && (
            <p className="px-2 py-1.5 text-sm text-zinc-500 dark:text-zinc-400">
              No tables found.
            </p>
          )}
        </nav>
      </aside>
      <section className="min-w-0 flex-1 overflow-hidden rounded-lg border border-zinc-200 bg-white dark:border-zinc-800 dark:bg-zinc-900">
        <Outlet />
      </section>
    </div>
  );
}
