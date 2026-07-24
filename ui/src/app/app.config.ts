import { APP_BASE_HREF, PlatformLocation } from '@angular/common';
import {
  ApplicationConfig,
  provideBrowserGlobalErrorListeners,
  provideZonelessChangeDetection,
} from '@angular/core';
import { provideRouter, withComponentInputBinding } from '@angular/router';

import { routes } from './app.routes';

/**
 * Derive the Angular Router base path from the document base href.
 *
 * The backend serves ``index.html`` with ``<base href="<ui_path>assets/">``
 * so that Angular's relative asset URLs resolve to the hashed bundle under
 * ``<ui_path>assets/``. The SPA itself, however, is mounted at ``<ui_path>``
 * (one level up), so the router base must be the *parent* of the asset base.
 * Reading it from ``document.baseURI`` at runtime keeps the SPA
 * location-independent — relocating it is a single server-side ``UI_PATH``
 * change with no rebuild.
 *
 * In dev (Vite-less Angular dev server) ``<base href>`` is ``/`` and there is
 * no ``assets/`` suffix, so the parent resolves to ``/`` as well.
 */
export function deriveAppBaseHref(platform: PlatformLocation): string {
  const baseHref = platform.getBaseHrefFromDOM() || '/';
  // Strip a trailing ``assets/`` (production) — the router lives one level up.
  const normalized = baseHref.replace(/assets\/?$/, '');
  return normalized || '/';
}

export const appConfig: ApplicationConfig = {
  providers: [
    provideBrowserGlobalErrorListeners(),
    provideZonelessChangeDetection(),
    provideRouter(routes, withComponentInputBinding()),
    {
      provide: APP_BASE_HREF,
      useFactory: deriveAppBaseHref,
      deps: [PlatformLocation],
    },
  ],
};
