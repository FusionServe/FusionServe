import { GRAPHQL_URL } from "@/lib/api";

// The bundled Strawberry GraphiQL IDE is served by the backend on GET
// requests against the GraphQL endpoint. We embed it directly via an
// iframe so users get the full official UI without dragging the
// graphiql React component (and its Monaco / codemirror deps) into our
// bundle.
export function GraphQLPage() {
  return (
    <section className="flex h-[calc(100vh-12rem)] flex-col gap-4">
      <header className="space-y-1">
        <h1 className="text-2xl font-semibold tracking-tight">GraphQL</h1>
        <p className="text-zinc-600 dark:text-zinc-400">
          GraphiQL IDE served by the backend at{" "}
          <a
            href={GRAPHQL_URL}
            target="_blank"
            rel="noreferrer"
            className="font-mono text-blue-600 hover:underline dark:text-blue-400"
          >
            {GRAPHQL_URL}
          </a>
        </p>
      </header>
      <iframe
        title="GraphiQL"
        src={GRAPHQL_URL}
        className="flex-1 rounded-lg border border-zinc-200 bg-white dark:border-zinc-800 dark:bg-zinc-950"
      />
    </section>
  );
}
