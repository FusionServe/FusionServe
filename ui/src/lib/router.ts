import { createBrowserHistory, createRouter } from "@tanstack/react-router";

import { routeTree } from "../routes";

/**
 * Application router (browser-history / path routing).
 *
 * The SPA is mounted under a base path that differs between dev (``/``) and
 * production (``settings.ui_path``, e.g. ``/api/-/``). Rather than hardcode
 * it, the basepath is read from ``document.baseURI`` — which reflects the
 * ``<base href>`` in ``index.html`` (``/`` in dev; rewritten to ``ui_path``
 * by the backend in production). This keeps the SPA location-independent: a
 * relocated deployment only changes the server-side ``UI_PATH``.
 *
 * Exported (rather than created inline in ``main.tsx``) so non-component code
 * — notably the OIDC callback handler in ``lib/auth`` — can drive navigation.
 */
export const basepath = new URL(document.baseURI).pathname;

export const router = createRouter({
  routeTree,
  history: createBrowserHistory(),
  basepath,
  defaultPreload: "intent",
});

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}
