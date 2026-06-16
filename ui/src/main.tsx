import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { RouterProvider } from "@tanstack/react-router";

import "./styles.css";
import { AuthProvider } from "./lib/auth";
import { RuntimeConfigProvider } from "./lib/runtimeConfig";
import { router } from "./lib/router";

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
