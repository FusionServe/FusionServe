import {
  ChangeDetectionStrategy,
  Component,
  computed,
  effect,
  input,
  output,
  signal,
} from '@angular/core';
import { FormsModule } from '@angular/forms';

import { type ColumnMeta, formatValue } from '../../lib/data-schema';

const INPUT_CLASS =
  'w-full rounded border border-transparent bg-transparent px-1 py-0.5 text-zinc-900 outline-none focus:border-blue-400 focus:bg-white dark:text-zinc-100 dark:focus:bg-zinc-900';

/**
 * Inline editable cell.
 *
 * Renders the appropriate control for the column's editor kind (checkbox for
 * boolean, select for enum, otherwise a typed text/number input) over a local
 * signal draft. Commits on change (booleans/enums/``commitOnChange``) or on
 * blur / Enter (text/number), emitting the raw value for the parent to coerce.
 */
@Component({
  selector: 'app-editable-cell',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [FormsModule],
  template: `
    @switch (col().editor) {
      @case ('boolean') {
        <input
          type="checkbox"
          class="size-4 accent-blue-600"
          [checked]="value() === true || value() === 'true'"
          (change)="commit($any($event.target).checked)"
        />
      }
      @case ('enum') {
        <select
          [ngModel]="value() == null ? '' : stringValue()"
          (ngModelChange)="commit($event)"
          [class]="inputClass"
        >
          @if (col().nullable) {
            <option value="">—</option>
          }
          @for (v of col().enumValues ?? []; track v) {
            <option [value]="v">{{ v }}</option>
          }
        </select>
      }
      @default {
        <input
          [type]="numeric() ? 'number' : 'text'"
          [step]="col().editor === 'integer' ? 1 : 'any'"
          [ngModel]="draft()"
          (ngModelChange)="onInput($event)"
          [placeholder]="placeholder() ?? ''"
          [class]="inputClass"
          (blur)="onBlur()"
          (keydown.enter)="$any($event.target).blur()"
        />
      }
    }
  `,
})
export class EditableCell {
  readonly col = input.required<ColumnMeta>();
  readonly value = input<unknown>('');
  readonly commitOnChange = input(false);
  readonly placeholder = input<string | undefined>(undefined);
  readonly commit$ = output<unknown>({ alias: 'commit' });

  protected readonly inputClass = INPUT_CLASS;
  protected readonly draft = signal('');
  protected readonly numeric = computed(
    () => this.col().editor === 'integer' || this.col().editor === 'number',
  );
  protected readonly stringValue = computed(() => String(this.value() ?? ''));

  constructor() {
    // Reset the draft whenever the incoming value changes.
    effect(() => this.draft.set(formatValue(this.value())));
  }

  protected commit(raw: unknown): void {
    this.commit$.emit(raw);
  }

  protected onInput(v: string): void {
    this.draft.set(v);
    if (this.commitOnChange()) this.commit$.emit(v);
  }

  protected onBlur(): void {
    if (!this.commitOnChange() && this.draft() !== formatValue(this.value())) {
      this.commit$.emit(this.draft());
    }
  }
}
