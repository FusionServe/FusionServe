import {
  Outlet,
  createRootRoute,
  createRoute,
} from "@tanstack/react-router";

import { AppLayout } from "./components/AppLayout";
import { LandingPage } from "./pages/LandingPage";
import { OpenAPIPage } from "./pages/OpenAPIPage";
import { GraphQLPage } from "./pages/GraphQLPage";

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
  component: GraphQLPage,
});

export const routeTree = rootRoute.addChildren([
  indexRoute,
  openapiRoute,
  graphqlRoute,
]);
