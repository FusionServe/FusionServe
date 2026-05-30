import { type ReactNode, Suspense, lazy } from "react";
import {
  Outlet,
  createRootRoute,
  createRoute,
} from "@tanstack/react-router";

import { AppLayout } from "./components/AppLayout";
import { LandingPage } from "./pages/LandingPage";

// Both API explorers pull in heavy dependencies (GraphiQL → Monaco;
// OpenAPI → swagger-ui-react), so they're lazy-loaded and only download
// when their route is visited, keeping the initial bundle small.
const GraphQLPage = lazy(() =>
  import("./pages/GraphQLPage").then((m) => ({ default: m.GraphQLPage })),
);

const OpenAPIPage = lazy(() =>
  import("./pages/OpenAPIPage").then((m) => ({ default: m.OpenAPIPage })),
);

function LazyRoute({ children, label }: { children: ReactNode; label: string }) {
  return (
    <Suspense
      fallback={
        <div className="flex h-[calc(100vh-9rem)] items-center justify-center text-sm text-zinc-500 dark:text-zinc-400">
          {label}
        </div>
      }
    >
      {children}
    </Suspense>
  );
}

function GraphQLRouteComponent() {
  return (
    <LazyRoute label="Loading GraphiQL…">
      <GraphQLPage />
    </LazyRoute>
  );
}

function OpenAPIRouteComponent() {
  return (
    <LazyRoute label="Loading Swagger UI…">
      <OpenAPIPage />
    </LazyRoute>
  );
}

const rootRoute = createRootRoute({
  component: () => (
    <AppLayout>
      <Outlet />
    </AppLayout>
  ),
});

const indexRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/",
  component: LandingPage,
});

const openapiRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/openapi",
  component: OpenAPIRouteComponent,
});

const graphqlRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/graphql",
  component: GraphQLRouteComponent,
});

export const routeTree = rootRoute.addChildren([
  indexRoute,
  openapiRoute,
  graphqlRoute,
]);
