import { OPENAPI_URL, SWAGGER_URL } from "@/lib/api";

// The OpenAPI reference is rendered by the backend via Litestar's
// ``SwaggerRenderPlugin`` (mounted on the OpenAPI router at
// ``/api/swagger``). We embed it directly via an iframe so users get
// the full official UI without dragging Swagger UI / Scalar React
// components and their transitive deps into our bundle — same approach
// as ``GraphQLPage`` does for GraphiQL.
export function OpenAPIPage() {
  return (
    <section className="flex h-[calc(100vh-12rem)] flex-col gap-4">
      <header className="space-y-1">
        <h1 className="text-2xl font-semibold tracking-tight">OpenAPI reference</h1>
        <p className="text-zinc-600 dark:text-zinc-400">
          Swagger UI served by the backend at{" "}
          <a
            href={SWAGGER_URL}
            target="_blank"
            rel="noreferrer"
            className="font-mono text-blue-600 hover:underline dark:text-blue-400"
          >
            {SWAGGER_URL}
          </a>
          . Raw spec at{" "}
          <a
            href={OPENAPI_URL}
            target="_blank"
            rel="noreferrer"
            className="font-mono text-blue-600 hover:underline dark:text-blue-400"
          >
            {OPENAPI_URL}
          </a>
          .
        </p>
      </header>
      <iframe
        title="Swagger UI"
        src={SWAGGER_URL}
        className="flex-1 rounded-lg border border-zinc-200 bg-white dark:border-zinc-800 dark:bg-zinc-950"
      />
    </section>
  );
}
