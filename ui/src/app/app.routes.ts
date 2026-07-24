import { Routes } from '@angular/router';

/**
 * Application routes (browser-history / path routing).
 *
 * The two API explorers (OpenAPI viewer, GraphQL IDE) and the data feature
 * pull in heavy dependencies (Stoplight Elements, Altair, the introspection
 * grid), so they are lazily loaded and only downloaded when their route is
 * visited, keeping the initial bundle small.
 */
export const routes: Routes = [
  {
    path: '',
    pathMatch: 'full',
    loadComponent: () => import('./pages/landing').then((m) => m.LandingPage),
  },
  {
    path: 'openapi',
    loadComponent: () => import('./pages/openapi').then((m) => m.OpenApiPage),
  },
  {
    path: 'graphql',
    loadComponent: () => import('./pages/graphql-page').then((m) => m.GraphqlPage),
  },
  {
    path: 'data',
    loadComponent: () => import('./pages/data/data-layout').then((m) => m.DataLayout),
    children: [
      {
        path: '',
        pathMatch: 'full',
        loadComponent: () => import('./pages/data/data-index').then((m) => m.DataIndex),
      },
      {
        path: ':table',
        loadComponent: () => import('./pages/data/data-table').then((m) => m.DataTablePage),
      },
    ],
  },
];
