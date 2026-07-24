import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  HostListener,
  computed,
  effect,
  inject,
  input,
  output,
  signal,
} from '@angular/core';
import { FormsModule } from '@angular/forms';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { lucideFilter } from '@ng-icons/lucide';

import type {
  ActiveFilter,
  ColumnMeta,
  FilterColumnMeta,
  OperatorMeta,
} from '../../lib/data-schema';

/** Human-friendly labels for the introspected operator names (fallback: humanized). */
const OPERATOR_LABELS: Record<string, string> = {
  exact: 'equals',
  neq: 'not equals',
  isNull: 'is null',
  inList: 'in list',
  notInList: 'not in list',
  contains: 'contains',
  iContains: 'contains (i)',
  startsWith: 'starts with',
  iStartsWith: 'starts with (i)',
  endsWith: 'ends with',
  iEndsWith: 'ends with (i)',
  regex: 'matches regex',
  iRegex: 'matches regex (i)',
  gt: 'greater than',
  gte: 'greater or equal',
  lt: 'less than',
  lte: 'less or equal',
  range: 'between',
};

function operatorLabel(name: string): string {
  if (OPERATOR_LABELS[name]) return OPERATOR_LABELS[name];
  return name
    .replace(/([a-z0-9])([A-Z])/g, '$1 $2')
    .replace(/_/g, ' ')
    .toLowerCase();
}

const FIELD_CLASS =
  'w-full rounded border border-zinc-300 bg-white px-1.5 py-1 text-sm outline-none focus:border-blue-400 dark:border-zinc-600 dark:bg-zinc-900';

/**
 * Per-column filter control: a funnel toggle in a column header that opens a
 * popover with an operator dropdown (all introspected operators for the
 * column's type) and a value input shaped to the operator (text/number/date,
 * boolean/enum select, comma-separated list, or a between-range pair).
 */
@Component({
  selector: 'app-column-filter',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [FormsModule, NgIcon],
  providers: [provideIcons({ lucideFilter })],
  host: { class: 'relative inline-block' },
  template: `
    <button
      type="button"
      [title]="isActive() ? 'Edit filter' : 'Filter column'"
      class="ml-1 rounded p-0.5 align-middle transition-colors hover:bg-zinc-200 dark:hover:bg-zinc-700"
      [class.text-blue-600]="isActive()"
      [class.dark:text-blue-400]="isActive()"
      [class.text-zinc-400]="!isActive()"
      (click)="open.set(!open())"
    >
      <ng-icon name="lucideFilter" size="0.875rem" [class.fill-current]="isActive()" />
    </button>

    @if (open()) {
      <div
        class="absolute left-0 top-full z-30 mt-1 w-60 rounded-md border border-zinc-200 bg-white p-2.5 text-left shadow-lg dark:border-zinc-700 dark:bg-zinc-800"
      >
        <label class="mb-1 block text-[11px] font-medium uppercase tracking-wide text-zinc-400">
          Operator
        </label>
        <select [(ngModel)]="op" [class]="fieldClass" class="mb-2">
          @for (o of operators(); track o.name) {
            <option [value]="o.name">{{ label(o.name) }}</option>
          }
        </select>

        @if (opMeta(); as m) {
          @if (m.name === 'isNull' || m.valueKind === 'boolean') {
            <select [(ngModel)]="bool" [class]="fieldClass">
              <option [ngValue]="true">true</option>
              <option [ngValue]="false">false</option>
            </select>
          } @else if (m.valueKind === 'range') {
            <div class="flex items-center gap-1.5">
              <input
                [type]="rangeType(m)"
                [(ngModel)]="rangeStart"
                placeholder="start"
                [class]="fieldClass"
              />
              <span class="text-xs text-zinc-400">–</span>
              <input
                [type]="rangeType(m)"
                [(ngModel)]="rangeEnd"
                placeholder="end"
                [class]="fieldClass"
              />
            </div>
          } @else if (m.valueKind === 'list') {
            <input
              type="text"
              [(ngModel)]="text"
              placeholder="comma,separated,values"
              [class]="fieldClass"
              (keydown.enter)="apply()"
            />
          } @else if (column().editor === 'enum') {
            <select [(ngModel)]="text" [class]="fieldClass">
              <option value="">—</option>
              @for (v of column().enumValues ?? []; track v) {
                <option [value]="v">{{ v }}</option>
              }
            </select>
          } @else {
            <input
              [type]="scalarType(m)"
              [step]="m.scalarType === 'Int' ? 1 : 'any'"
              [(ngModel)]="text"
              placeholder="value"
              [class]="fieldClass"
              (keydown.enter)="apply()"
            />
          }
        }

        <div class="mt-2.5 flex justify-end gap-2">
          <button
            type="button"
            class="rounded px-2 py-1 text-xs font-medium text-zinc-500 hover:bg-zinc-100 dark:hover:bg-zinc-700"
            (click)="doClear()"
          >
            Clear
          </button>
          <button
            type="button"
            class="rounded-md bg-blue-600 px-2.5 py-1 text-xs font-medium text-white transition-colors hover:bg-blue-700"
            (click)="apply()"
          >
            Apply
          </button>
        </div>
      </div>
    }
  `,
})
export class ColumnFilter {
  private readonly host = inject(ElementRef<HTMLElement>);

  readonly column = input.required<ColumnMeta>();
  readonly filterColumn = input.required<FilterColumnMeta>();
  readonly active = input<ActiveFilter | undefined>(undefined);
  readonly apply$ = output<ActiveFilter>({ alias: 'apply' });
  readonly clear$ = output<void>({ alias: 'clear' });

  protected readonly fieldClass = FIELD_CLASS;
  protected readonly open = signal(false);
  protected readonly operators = computed(() => this.filterColumn().operators);

  protected readonly op = signal('');
  protected readonly text = signal('');
  protected readonly rangeStart = signal('');
  protected readonly rangeEnd = signal('');
  protected readonly bool = signal(false);

  protected readonly opMeta = computed<OperatorMeta | undefined>(() => {
    const ops = this.operators();
    return ops.find((o) => o.name === this.op()) ?? ops[0];
  });
  protected readonly isActive = computed(() => this.active() !== undefined);

  constructor() {
    // Seed the draft controls from the active filter whenever it changes.
    effect(() => {
      const a = this.active();
      const ops = this.operators();
      this.op.set(a?.op ?? ops[0]?.name ?? '');
      this.text.set(this.initialText(a));
      const r = this.initialRange(a);
      this.rangeStart.set(r.start);
      this.rangeEnd.set(r.end);
      this.bool.set(a?.value === true || a?.value === 'true');
    });
  }

  @HostListener('document:mousedown', ['$event'])
  protected onDocMouseDown(e: MouseEvent): void {
    if (this.open() && !this.host.nativeElement.contains(e.target as Node)) {
      this.open.set(false);
    }
  }

  @HostListener('document:keydown.escape')
  protected onEscape(): void {
    this.open.set(false);
  }

  protected label(name: string): string {
    return operatorLabel(name);
  }

  protected scalarType(op: OperatorMeta): string {
    const numeric = op.scalarType === 'Int' || op.scalarType === 'Float' || op.scalarType === 'Decimal';
    if (numeric) return 'number';
    if (this.column().editor === 'date') return 'date';
    if (this.column().editor === 'datetime') return 'datetime-local';
    return 'text';
  }

  protected rangeType(op: OperatorMeta): string {
    return op.scalarType === 'Int' || op.scalarType === 'Float' || op.scalarType === 'Decimal'
      ? 'number'
      : 'text';
  }

  protected apply(): void {
    const m = this.opMeta();
    if (!m) return;
    let value: unknown;
    switch (m.valueKind) {
      case 'boolean':
        value = this.bool();
        break;
      case 'range':
        value = { start: this.rangeStart(), end: this.rangeEnd() };
        break;
      default:
        value = this.text();
    }
    this.apply$.emit({ op: m.name, value });
    this.open.set(false);
  }

  protected doClear(): void {
    this.clear$.emit();
    this.text.set('');
    this.rangeStart.set('');
    this.rangeEnd.set('');
    this.bool.set(false);
    this.open.set(false);
  }

  private initialText(active: ActiveFilter | undefined): string {
    if (!active) return '';
    if (Array.isArray(active.value)) return active.value.join(',');
    if (typeof active.value === 'object' && active.value !== null) return '';
    if (typeof active.value === 'boolean') return '';
    return String(active.value ?? '');
  }

  private initialRange(active: ActiveFilter | undefined): { start: string; end: string } {
    if (
      active &&
      typeof active.value === 'object' &&
      active.value !== null &&
      !Array.isArray(active.value)
    ) {
      const r = active.value as { start?: unknown; end?: unknown };
      return { start: String(r.start ?? ''), end: String(r.end ?? '') };
    }
    return { start: '', end: '' };
  }
}
