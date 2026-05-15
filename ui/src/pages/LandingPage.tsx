import { Link } from "@tanstack/react-router";

interface FeatureCardProps {
  to: string;
  title: string;
  description: string;
}

function FeatureCard({ to, title, description }: FeatureCardProps) {
  return (
    <Link
      to={to}
      className="block rounded-lg border border-zinc-200 bg-white p-6 transition-colors hover:border-zinc-400 hover:bg-zinc-50 dark:border-zinc-800 dark:bg-zinc-900 dark:hover:border-zinc-600 dark:hover:bg-zinc-800"
    >
      <p className="font-medium text-zinc-900 dark:text-zinc-50">{title}</p>
      <p className="mt-1 text-sm text-zinc-600 dark:text-zinc-400">
        {description}
      </p>
    </Link>
  );
}

export function LandingPage() {
  return (
    <section className="space-y-8">
      <header className="space-y-3">
        <h1 className="text-3xl font-semibold tracking-tight">FusionServe</h1>
        <p className="max-w-2xl text-zinc-600 dark:text-zinc-400">
          REST and GraphQL endpoints are generated automatically from the
          configured PostgreSQL schema. Use the links below — or the
          navigation above — to explore the live API surface.
        </p>
      </header>

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <FeatureCard
          to="/openapi"
          title="OpenAPI reference"
          description="Interactive Scalar viewer for the auto-generated OpenAPI 3.1 document."
        />
        <FeatureCard
          to="/graphql"
          title="GraphQL playground"
          description="Strawberry GraphiQL IDE backed by the same introspected schema."
        />
      </div>
    </section>
  );
}
