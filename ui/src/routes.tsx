import { Suspense, lazy } from "react";
import {
  Outlet,
  createRootRoute,
  createRoute,
} from "@tanstack/react-router";

import { AppLayout } from "./components/AppLayout";
import { LandingPage } from "./pages/LandingPage";
import { OpenAPIPage } from "./pages/OpenAPIPage";

// GraphiQL pulls in Monaco and is several MB; lazy-load it so it only
// downloads when the ``/graphql`` route is visited, keeping the initial
// bundle small for the other pages.
const GraphQLPage = lazy(() =>
  import("./pages/GraphQLPage").then((m) => ({ default: m.GraphQLPage })),
);

function GraphQLRouteComponent() {
  return (
    <Suspense
      fallback={
        <div className="flex h-[calc(100vh-9rem)] items-center justify-center text-sm text-zinc-500 dark:text-zinc-400">
          Loading GraphiQL…
        </div>
      }
    >
      <GraphQLPage />
    </Suspense>
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
  component: OpenAPIPage,
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
