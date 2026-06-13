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

// The data feature pulls in TanStack Table + Query; lazy-load it too.
const DataLayout = lazy(() =>
  import("./pages/data/DataLayout").then((m) => ({ default: m.DataLayout })),
);
const DataIndex = lazy(() =>
  import("./pages/data/DataIndex").then((m) => ({ default: m.DataIndex })),
);
const DataTablePage = lazy(() =>
  import("./pages/data/DataTablePage").then((m) => ({ default: m.DataTablePage })),
);

function LazyRoute({ children, label }: { children: ReactNode; label: string }) {
  return (
    <Suspense
      fallback={
        <div className="flex h-full items-center justify-center text-sm text-zinc-500 dark:text-zinc-400">
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

function DataLayoutRouteComponent() {
  return (
    <LazyRoute label="Loading data editor…">
      <DataLayout />
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

// ``/data`` layout (left nav + Outlet) with an index placeholder and a
// deep-linkable ``/data/$table`` grid. ``DataLayout`` mounts the
// QueryClientProvider shared by the index and table routes.
const dataRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/data",
  component: DataLayoutRouteComponent,
});

const dataIndexRoute = createRoute({
  getParentRoute: () => dataRoute,
  path: "/",
  component: DataIndex,
});

const dataTableRoute = createRoute({
  getParentRoute: () => dataRoute,
  path: "$table",
  component: DataTablePage,
});

export const routeTree = rootRoute.addChildren([
  indexRoute,
  openapiRoute,
  graphqlRoute,
  dataRoute.addChildren([dataIndexRoute, dataTableRoute]),
]);
