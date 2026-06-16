import path from "node:path";
import { fileURLToPath } from "node:url";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// FusionServe SPA build configuration.
//
// Built assets are emitted into ``../src/fusionserve/web/dist`` so the
// Python wheel (which packages everything under ``src/fusionserve/``)
// ships the SPA without extra packaging configuration.
//
// Asset URLs in the emitted ``index.html`` are RELATIVE (``./assets/...``).
// The SPA uses browser-history (path) routing, so those relative URLs are
// resolved against the document's ``<base href>`` rather than the current
// deep route. ``index.html`` ships ``<base href="/">``; in production the
// backend (``fusionserve.ui.build_spa_route_handler``) rewrites it to
// ``settings.ui_path`` before serving, so the chunks resolve to
// ``<ui_path>/assets/...`` for any route. Relocating the SPA therefore
// stays a single Python-side change (override ``settings.ui_path``) — no JS
// rebuild or sync constant is required.
//
// In production the SPA is served by Litestar (built by
// ``fusionserve.ui.build_spa_route_handler``): an assets static router at
// ``<ui_path>assets`` plus a base-href-injecting ``index.html`` handler that
// also acts as the deep-link fallback. Users typically arrive via the
// ``<base_path>/`` -> ``settings.ui_path`` redirect emitted by
// ``fusionserve.ui.RedirectRenderPlugin``.
//
// Dev workflow: ``pnpm run dev`` in ``ui/`` starts Vite's dev server at
// ``http://localhost:5173/`` and proxies ``/api/*`` requests to the
// backend at ``http://localhost:8001``. In dev the SPA is reached at the
// dev-server root (base ``/``); Vite's history fallback serves
// ``index.html`` for client-side deep links.
const __dirname = path.dirname(fileURLToPath(import.meta.url));
const bundleDir = path.resolve(__dirname, "../src/fusionserve/web/dist");

export default defineConfig(({ command }) => ({
  // ``base`` is applied at BOTH build time (rewriting asset URLs in
  // emitted ``index.html``) and dev time (the dev server serves
  // ``index.html`` at ``base``). At build time we want RELATIVE URLs
  // so the SPA is location-independent; at dev time we want the
  // dev-server root so ``http://localhost:5173/`` loads the app.
  base: command === "build" ? "./" : "/",
  // ``swagger-ui-react`` pulls in ``swagger-client`` / ``buffer`` and other
  // packages that reference the Node ``global`` object, which doesn't exist
  // in the browser. Map it to ``globalThis`` so those modules load under
  // Vite (both dev and build).
  define: {
    global: "globalThis",
  },
  plugins: [tailwindcss(), react()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  server: {
    // ``pnpm run dev`` proxies backend traffic to the locally running
    // Litestar app (default port 8001 per DEVELOPMENT.md). Anything
    // under ``/api`` covers OpenAPI surfaces (Swagger, Scalar, the raw
    // JSON document), the REST CRUD endpoints, and the GraphQL endpoint
    // — all reachable transparently from the SPA during development.
    // ``/.well-known`` is forwarded too: the client-configuration document
    // (``/.well-known/configuration``) lives outside ``base_path``.
    proxy: {
      "/api": { target: "http://localhost:8001", changeOrigin: true },
      "/.well-known": { target: "http://localhost:8001", changeOrigin: true },
    },
  },
  build: {
    // Emit the SPA directly into the Python package so ``uv_build``
    // packages it into the wheel automatically. ``outDir`` lives
    // outside the project root, which by default disables Vite's
    // "empty before build" safety — re-enable it so repeated builds
    // don't accumulate stale hashed chunks under
    // ``src/fusionserve/web/dist``.
    outDir: bundleDir,
    emptyOutDir: true,
  },
}));
