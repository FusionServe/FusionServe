import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import {
  RouterProvider,
  createHashHistory,
  createRouter,
} from "@tanstack/react-router";

import "./styles.css";
import { AuthProvider } from "./lib/auth";
import { RuntimeConfigProvider } from "./lib/runtimeConfig";
import { routeTree } from "./routes";

const router = createRouter({
  routeTree,
  history: createHashHistory(),
  defaultPreload: "intent",
});

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}

const rootElement = document.getElementById("root");
if (rootElement === null) {
  throw new Error("Root element #root not found in index.html");
}

createRoot(rootElement).render(
  <StrictMode>
    <RuntimeConfigProvider>
      <AuthProvider>
        <RouterProvider router={router} />
      </AuthProvider>
    </RuntimeConfigProvider>
  </StrictMode>,
);
