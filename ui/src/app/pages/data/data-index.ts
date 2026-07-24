import { ChangeDetectionStrategy, Component } from '@angular/core';

/** Placeholder shown at ``/data`` before a table is selected. */
@Component({
  selector: 'app-data-index',
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: { class: 'flex h-full items-center justify-center' },
  template: `
    <div class="p-8 text-center text-sm text-zinc-500 dark:text-zinc-400">
      Select a table from the left to view and edit its rows.
    </div>
  `,
})
export class DataIndex {}
