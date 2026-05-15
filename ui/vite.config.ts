import path from "node:path";
import { fileURLToPath } from "node:url";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import litestar from "litestar-vite-plugin";
import { defineConfig } from "vite";

// FusionServe SPA build configuration.
//
// Built assets are emitted into ``../src/fusionserve/web/dist`` so the
// Python wheel (which packages everything under ``src/fusionserve/``)
// ships the SPA without extra packaging configuration.
//
// Asset URLs are anchored under ``/-/assets/`` — deliberately OUTSIDE
// ``settings.base_path`` (``/api``). The Litestar OpenAPI router
// mounted at ``base_path`` registers a ``<base_path>/{path:str}``
// not-found handler; any asset URL under ``base_path`` would be
// shadowed by it. Keeping assets at a top-level prefix sidesteps that
// entirely.
//
// The matching constant lives at
// ``fusionserve.config.Settings.ui_assets_path``; the Python and JS
// sides MUST be kept in sync (no runtime cross-check).
//
// The SPA itself is mounted at ``settings.ui_path`` (default ``/-/``)
// by ``litestar-vite``. Users typically arrive via the ``/api/`` ->
// ``settings.ui_path`` redirect emitted by
// ``fusionserve.ui.RedirectRenderPlugin``.
const __dirname = path.dirname(fileURLToPath(import.meta.url));
const bundleDir = path.resolve(__dirname, "../src/fusionserve/web/dist");

export default defineConfig({
  plugins: [
    tailwindcss(),
    react(),
    litestar({
      input: ["src/main.tsx"],
      assetUrl: "/-/assets/",
      bundleDir,
      resourceDir: "src",
    }),
  ],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  build: {
    // outDir lives outside the project root (inside the Python package),
    // which by default disables Vite's "empty before build" safety.
    // Re-enable it explicitly so repeated builds don't accumulate stale
    // hashed chunks in ``src/fusionserve/web/dist``.
    emptyOutDir: true,
    // Flatten Vite's default ``assets/`` sub-directory so hashed chunks
    // land directly under ``bundle_dir``. Combined with
    // ``base=/-/assets/`` (set via the litestar plugin's ``assetUrl``),
    // served URLs become ``/-/assets/<hash>.js`` instead of the doubled
    // ``/-/assets/assets/<hash>.js`` we'd otherwise see.
    assetsDir: "",
  },
});
