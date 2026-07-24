import { ChangeDetectionStrategy, Component, type OnInit, inject } from '@angular/core';
import { RouterLink, RouterLinkActive, RouterOutlet } from '@angular/router';

import { DataSchemaService } from './data-schema.service';

/**
 * Layout for the data-editing feature: a left nav listing every database table
 * (discovered from the ``*Connection`` query fields) and a ``<router-outlet>``
 * rendering the selected table's grid.
 */
@Component({
  selector: 'app-data-layout',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, RouterLinkActive, RouterOutlet],
  host: { class: 'block h-full' },
  template: `
    <div class="flex h-full gap-4">
      <aside
        class="w-60 shrink-0 overflow-y-auto rounded-lg border border-zinc-200 bg-white p-2 dark:border-zinc-800 dark:bg-zinc-900"
      >
        <p class="px-2 py-1.5 text-xs font-semibold uppercase tracking-wide text-zinc-400 dark:text-zinc-500">
          Tables
        </p>
        @if (schemas.loading()) {
          <p class="px-2 py-1.5 text-sm text-zinc-500 dark:text-zinc-400">Loading schema…</p>
        }
        @if (schemas.error(); as err) {
          <p class="px-2 py-1.5 text-sm text-red-600 dark:text-red-400">{{ err }}</p>
        }
        <nav class="flex flex-col">
          @for (table of schemas.schema()?.tables ?? []; track table.name) {
            <a
              [routerLink]="['/data', table.name]"
              routerLinkActive="bg-blue-50 font-medium text-blue-700 dark:bg-blue-950 dark:text-blue-300"
              [title]="table.label"
              class="truncate rounded-md px-2 py-1.5 text-sm text-zinc-600 transition-colors hover:bg-zinc-100 hover:text-zinc-900 dark:text-zinc-400 dark:hover:bg-zinc-800 dark:hover:text-zinc-100"
            >
              {{ table.label }}
            </a>
          }
          @if (schemas.schema()?.tables?.length === 0) {
            <p class="px-2 py-1.5 text-sm text-zinc-500 dark:text-zinc-400">No tables found.</p>
          }
        </nav>
      </aside>
      <section
        class="min-w-0 flex-1 overflow-hidden rounded-lg border border-zinc-200 bg-white dark:border-zinc-800 dark:bg-zinc-900"
      >
        <router-outlet />
      </section>
    </div>
  `,
})
export class DataLayout implements OnInit {
  protected readonly schemas = inject(DataSchemaService);

  ngOnInit(): void {
    void this.schemas.load().catch(() => {
      /* error surfaced via schemas.error() */
    });
  }
}
