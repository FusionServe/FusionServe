import { ChangeDetectionStrategy, Component } from '@angular/core';
import { RouterLink } from '@angular/router';

interface FeatureCard {
  path: string;
  title: string;
  description: string;
}

const CARDS: readonly FeatureCard[] = [
  {
    path: '/openapi',
    title: 'OpenAPI reference',
    description: 'Interactive Stoplight Elements viewer for the auto-generated OpenAPI 3.1 document.',
  },
  {
    path: '/graphql',
    title: 'GraphQL playground',
    description: 'Altair GraphQL IDE backed by the same introspected schema.',
  },
  {
    path: '/data',
    title: 'Data editor',
    description: 'Browse, filter and edit every introspected table with live REST + GraphQL access.',
  },
];

@Component({
  selector: 'app-landing',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink],
  template: `
    <section class="space-y-8">
      <header class="space-y-3">
        <h1 class="text-3xl font-semibold tracking-tight">FusionServe</h1>
        <p class="max-w-2xl text-zinc-600 dark:text-zinc-400">
          REST and GraphQL endpoints are generated automatically from the configured PostgreSQL
          schema. Use the links below — or the navigation above — to explore the live API surface.
        </p>
      </header>

      <div class="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
        @for (card of cards; track card.path) {
          <a
            [routerLink]="card.path"
            class="block rounded-lg border border-zinc-200 bg-white p-6 transition-colors hover:border-zinc-400 hover:bg-zinc-50 dark:border-zinc-800 dark:bg-zinc-900 dark:hover:border-zinc-600 dark:hover:bg-zinc-800"
          >
            <p class="font-medium text-zinc-900 dark:text-zinc-50">{{ card.title }}</p>
            <p class="mt-1 text-sm text-zinc-600 dark:text-zinc-400">{{ card.description }}</p>
          </a>
        }
      </div>
    </section>
  `,
})
export class LandingPage {
  protected readonly cards = CARDS;
}
