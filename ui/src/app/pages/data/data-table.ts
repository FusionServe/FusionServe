import {
  ChangeDetectionStrategy,
  Component,
  type ElementRef,
  computed,
  effect,
  inject,
  input,
  signal,
  viewChild,
} from '@angular/core';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { lucideChevronRight, lucideTrash2 } from '@ng-icons/lucide';
import { form } from '@angular/forms/signals';

import { AuthService } from '../../core/auth.service';
import { GraphQLRequestError, GraphqlService } from '../../core/graphql.client';
import {
  type ActiveFilter,
  type ColumnMeta,
  type DataSchema,
  type TableMeta,
  buildCreateMutation,
  buildDeleteMutation,
  buildFilterValue,
  buildListQuery,
  buildOrderValue,
  buildUpdateMutation,
  coerceValue,
  formatValue,
  pkVariables,
  rowKey,
} from '../../lib/data-schema';
import { ColumnFilter } from './column-filter';
import { DataSchemaService } from './data-schema.service';
import { EditableCell } from './editable-cell';
import { RelationCacheService } from './relation-cache.service';
import { RelationDetail } from './relation-detail';

/** Rows fetched per page (cursor ``first`` / offset ``limit``). */
const PAGE_SIZE = 50;

type Row = Record<string, unknown>;

interface SortState {
  id: string;
  desc: boolean;
}

function humanizeColumn(name: string): string {
  return name
    .replace(/_/g, ' ')
    .replace(/([a-z0-9])([A-Z])/g, '$1 $2')
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

function errorMessage(e: unknown): string {
  if (e instanceof GraphQLRequestError) return e.message;
  if (e instanceof Error) return e.message;
  return String(e);
}

@Component({
  selector: 'app-data-table',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon, ColumnFilter, EditableCell, RelationDetail],
  providers: [provideIcons({ lucideChevronRight, lucideTrash2 })],
  host: { class: 'block h-full' },
  template: `
    @if (schemas.loading()) {
      <div class="flex h-full items-center justify-center p-8 text-sm text-zinc-500 dark:text-zinc-400">
        Loading schema…
      </div>
    } @else if (!meta()) {
      <div class="flex h-full items-center justify-center p-8 text-sm text-zinc-500 dark:text-zinc-400">
        Unknown table “{{ table() }}”.
      </div>
    } @else {
      <div class="flex h-full flex-col">
        <!-- Toolbar -->
        <div
          class="flex items-center justify-between gap-3 border-b border-zinc-200 px-4 py-2.5 dark:border-zinc-800"
        >
          <div class="min-w-0">
            <h1 class="truncate text-sm font-semibold text-zinc-900 dark:text-zinc-50">
              {{ meta()!.label }}
            </h1>
            <p class="text-xs text-zinc-500 dark:text-zinc-400">
              {{ total() }} row{{ total() === 1 ? '' : 's' }}
            </p>
          </div>
          <div class="flex items-center gap-2">
            @if (isFetching() && !isFetchingNext()) {
              <span class="text-xs text-zinc-400">Loading…</span>
            }
            @if (activeFilterCount() > 0) {
              <button
                type="button"
                class="rounded-md border border-zinc-300 px-2.5 py-1 text-xs font-medium text-zinc-700 transition-colors hover:bg-zinc-100 dark:border-zinc-700 dark:text-zinc-200 dark:hover:bg-zinc-800"
                (click)="clearFilters()"
              >
                Clear {{ activeFilterCount() }} filter{{ activeFilterCount() === 1 ? '' : 's' }}
              </button>
            }
            @if (meta()!.create) {
              <button
                type="button"
                [disabled]="!canWrite()"
                [title]="canWrite() ? 'Insert a new row' : 'Sign in to modify data'"
                class="rounded-md border border-zinc-300 px-2.5 py-1 text-xs font-medium text-zinc-700 transition-colors hover:bg-zinc-100 disabled:cursor-not-allowed disabled:opacity-50 dark:border-zinc-700 dark:text-zinc-200 dark:hover:bg-zinc-800"
                (click)="toggleCreating()"
              >
                {{ isCreating() ? 'Cancel' : 'Add row' }}
              </button>
            }
          </div>
        </div>

        @if (!canWrite()) {
          <div
            class="border-b border-amber-200 bg-amber-50 px-4 py-2 text-xs text-amber-800 dark:border-amber-900/50 dark:bg-amber-950/40 dark:text-amber-300"
          >
            You are not signed in — rows are read-only. Sign in (top-right badge) to modify data.
          </div>
        }
        @if (mutationError(); as err) {
          <div
            class="flex items-start justify-between gap-3 border-b border-red-200 bg-red-50 px-4 py-2 text-xs text-red-700 dark:border-red-900/50 dark:bg-red-950/40 dark:text-red-300"
          >
            <span class="break-words">{{ err }}</span>
            <button type="button" class="shrink-0 font-medium underline" (click)="mutationError.set(null)">
              Dismiss
            </button>
          </div>
        }
        @if (loadError(); as err) {
          <div
            class="border-b border-red-200 bg-red-50 px-4 py-2 text-xs text-red-700 dark:border-red-900/50 dark:bg-red-950/40 dark:text-red-300"
          >
            {{ err }}
          </div>
        }

        <!-- Table -->
        <div #scroll class="min-h-0 flex-1 overflow-auto">
          <table class="w-full border-collapse text-sm">
            <thead class="sticky top-0 z-10 bg-zinc-50 dark:bg-zinc-950">
              <tr>
                @if (canExpand()) {
                  <th class="w-8 border-b border-zinc-200 px-2 py-2 dark:border-zinc-800"></th>
                }
                @for (col of meta()!.columns; track col.name) {
                  <th
                    class="select-none whitespace-nowrap border-b border-zinc-200 px-3 py-2 text-left font-semibold text-zinc-600 dark:border-zinc-800 dark:text-zinc-300"
                  >
                    <span class="inline-flex items-center">
                      @if (canSort(col)) {
                        <button
                          type="button"
                          class="cursor-pointer bg-transparent p-0 font-semibold text-inherit"
                          [attr.aria-label]="'Sort by ' + header(col)"
                          (click)="toggleSort(col.name)"
                        >
                          {{ header(col) }}
                          @if (sorting()?.id === col.name) {
                            {{ sorting()!.desc ? ' ▼' : ' ▲' }}
                          }
                        </button>
                      } @else {
                        <span>{{ header(col) }}</span>
                      }
                      @if (filterColumn(col.name); as fc) {
                        <app-column-filter
                          [column]="col"
                          [filterColumn]="fc"
                          [active]="filters()[col.name]"
                          (apply)="applyFilter(col.name, $event)"
                          (clear)="clearFilter(col.name)"
                        />
                      }
                    </span>
                  </th>
                }
                @if (meta()!.remove) {
                  <th class="border-b border-zinc-200 px-3 py-2 dark:border-zinc-800"></th>
                }
              </tr>
            </thead>
            <tbody>
              @if (isCreating() && meta()!.create) {
                <tr class="bg-blue-50/50 dark:bg-blue-950/20">
                  @if (canExpand()) {
                    <td class="border-b border-zinc-100 dark:border-zinc-800"></td>
                  }
                  @for (col of meta()!.columns; track col.name) {
                    <td class="border-b border-zinc-100 px-3 py-1.5 dark:border-zinc-800">
                      @if (col.creatable) {
                        <app-editable-cell
                          [col]="col"
                          [value]="createModel()[col.name] ?? ''"
                          [commitOnChange]="true"
                          [placeholder]="col.nullable ? 'null' : ''"
                          (commit)="setCreateField(col.name, $event)"
                        />
                      } @else {
                        <span class="text-xs text-zinc-400">auto</span>
                      }
                    </td>
                  }
                  <td class="border-b border-zinc-100 px-3 py-1.5 text-right dark:border-zinc-800">
                    <button
                      type="button"
                      [disabled]="createPending()"
                      class="rounded-md bg-blue-600 px-2.5 py-1 text-xs font-medium text-white transition-colors hover:bg-blue-700 disabled:opacity-50"
                      (click)="submitCreate()"
                    >
                      Save
                    </button>
                  </td>
                </tr>
              }
              @for (row of rows(); track rowTrack(row)) {
                <tr class="hover:bg-zinc-50 dark:hover:bg-zinc-800/50">
                  @if (canExpand()) {
                    <td class="border-b border-zinc-100 px-2 py-1.5 align-top dark:border-zinc-800">
                      <button
                        type="button"
                        [attr.aria-label]="isExpanded(row) ? 'Collapse related' : 'Expand related'"
                        [attr.aria-expanded]="isExpanded(row)"
                        class="rounded p-0.5 text-zinc-400 transition-colors hover:bg-zinc-200 hover:text-zinc-700 dark:hover:bg-zinc-700 dark:hover:text-zinc-200"
                        (click)="toggleExpanded(row)"
                      >
                        <ng-icon
                          name="lucideChevronRight"
                          size="1rem"
                          class="transition-transform"
                          [class.rotate-90]="isExpanded(row)"
                        />
                      </button>
                    </td>
                  }
                  @for (col of meta()!.columns; track col.name) {
                    <td
                      class="max-w-xs truncate border-b border-zinc-100 px-3 py-1.5 align-top dark:border-zinc-800"
                    >
                      @if (canWrite() && col.updatable && !col.isPk) {
                        <app-editable-cell
                          [col]="col"
                          [value]="row[col.name]"
                          (commit)="commitEdit(row, col, $event)"
                        />
                      } @else {
                        <span class="block truncate text-zinc-600 dark:text-zinc-400" [title]="fmt(row[col.name])">
                          {{ fmt(row[col.name]) || '—' }}
                        </span>
                      }
                    </td>
                  }
                  @if (meta()!.remove) {
                    <td class="border-b border-zinc-100 px-3 py-1.5 text-right dark:border-zinc-800">
                      <button
                        type="button"
                        [disabled]="!canWrite() || deletePending()"
                        [title]="canWrite() ? 'Delete row' : 'Sign in to modify data'"
                        class="rounded p-1 text-zinc-400 transition-colors hover:bg-red-50 hover:text-red-600 disabled:cursor-not-allowed disabled:opacity-40 dark:hover:bg-red-950/40"
                        (click)="deleteRow(row)"
                      >
                        <ng-icon name="lucideTrash2" size="1rem" />
                      </button>
                    </td>
                  }
                </tr>
                @if (isExpanded(row)) {
                  <tr>
                    <td [attr.colspan]="colCount()" class="border-b border-zinc-200 p-0 dark:border-zinc-800">
                      <app-relation-detail [meta]="meta()!" [schema]="schema()!" [row]="row" />
                    </td>
                  </tr>
                }
              }
              @if (rows().length === 0 && !isFetching()) {
                <tr>
                  <td
                    [attr.colspan]="colCount()"
                    class="px-3 py-8 text-center text-sm text-zinc-500 dark:text-zinc-400"
                  >
                    No rows{{ activeFilterCount() > 0 ? ' match the active filters' : '' }}.
                  </td>
                </tr>
              }
            </tbody>
          </table>
          <!-- Infinite-scroll sentinel: observed to auto-load the next page. -->
          <div #sentinel class="h-px w-full"></div>
          @if (isFetchingNext()) {
            <div class="py-3 text-center text-xs text-zinc-400">Loading more…</div>
          }
        </div>

        <!-- Status bar -->
        <div
          class="flex items-center justify-between gap-3 border-t border-zinc-200 px-4 py-2 text-xs dark:border-zinc-800"
        >
          <div class="flex items-center gap-2 text-zinc-500 dark:text-zinc-400">
            @if (creatableCount() === 0) {
              <span class="text-zinc-400">(no insertable columns)</span>
            }
          </div>
          <div class="text-zinc-500 dark:text-zinc-400">
            Loaded {{ rows().length }} of {{ total() }} row{{ total() === 1 ? '' : 's' }}
            {{ !hasNextPage() && rows().length > 0 ? ' · end' : '' }}
          </div>
        </div>
      </div>
    }
  `,
})
export class DataTablePage {
  protected readonly schemas = inject(DataSchemaService);
  private readonly gql = inject(GraphqlService);
  private readonly auth = inject(AuthService);
  private readonly relationCache = inject(RelationCacheService);

  /** Route parameter (bound via ``withComponentInputBinding``). */
  readonly table = input.required<string>();

  protected readonly schema = this.schemas.schema;
  protected readonly meta = computed<TableMeta | undefined>(() =>
    this.schema()?.tables.find((t) => t.name === this.table()),
  );
  protected readonly canWrite = computed(() => this.auth.status() === 'authenticated');

  // ---- View state ----
  protected readonly sorting = signal<SortState | null>(null);
  protected readonly filters = signal<Record<string, ActiveFilter>>({});
  protected readonly expanded = signal<Set<string>>(new Set());
  protected readonly isCreating = signal(false);
  protected readonly mutationError = signal<string | null>(null);
  protected readonly loadError = signal<string | null>(null);

  // ---- Create-row form (Signal Forms over a dynamic column model) ----
  protected readonly createModel = signal<Record<string, string>>({});
  protected readonly createForm = form(this.createModel);
  protected readonly createPending = signal(false);
  protected readonly deletePending = signal(false);

  // ---- Rows / pagination ----
  protected readonly rows = signal<Row[]>([]);
  protected readonly total = signal(0);
  protected readonly hasNextPage = signal(false);
  protected readonly isFetching = signal(false);
  protected readonly isFetchingNext = signal(false);
  private endCursor: string | null = null;
  private offset = 0;
  private loadToken = 0;

  protected readonly activeFilterCount = computed(() => Object.keys(this.filters()).length);
  protected readonly creatableCount = computed(
    () => this.meta()?.columns.filter((c) => c.creatable).length ?? 0,
  );
  protected readonly canExpand = computed(
    () => (this.meta()?.relations.length ?? 0) > 0 && this.meta()?.pkLookup != null,
  );
  protected readonly colCount = computed(() => {
    const m = this.meta();
    if (!m) return 1;
    return (this.canExpand() ? 1 : 0) + m.columns.length + (m.remove ? 1 : 0);
  });

  private readonly order = computed(() => {
    const m = this.meta();
    const s = this.sorting();
    const schema = this.schema();
    if (!m || !s || !schema) return null;
    return buildOrderValue(m, s.id, s.desc, schema.sortAsc, schema.sortDesc);
  });
  private readonly filterValue = computed(() => {
    const m = this.meta();
    return m ? buildFilterValue(m, this.filters()) : null;
  });

  private readonly scrollRef = viewChild<ElementRef<HTMLDivElement>>('scroll');
  private readonly sentinelRef = viewChild<ElementRef<HTMLDivElement>>('sentinel');

  constructor() {
    // Auto-load the next page when the bottom sentinel scrolls into view. The
    // sentinel only exists once ``meta()`` resolves (it lives behind an
    // ``@if``), so bind the observer reactively — ``viewChild`` is a signal, so
    // this effect re-runs and (re)attaches when the element appears, and the
    // cleanup callback disconnects the previous observer / on destroy.
    effect((onCleanup) => {
      const sentinel = this.sentinelRef()?.nativeElement;
      if (!sentinel) return;
      const observer = new IntersectionObserver(
        (entries) => {
          if (entries[0]?.isIntersecting && this.hasNextPage() && !this.isFetchingNext()) {
            void this.loadNextPage();
          }
        },
        { root: this.scrollRef()?.nativeElement ?? null, rootMargin: '300px' },
      );
      observer.observe(sentinel);
      onCleanup(() => observer.disconnect());
    });

    // Reset per-table view state when the selected table changes.
    let lastTable: string | null = null;
    effect(() => {
      const m = this.meta();
      if (m && m.name !== lastTable) {
        lastTable = m.name;
        this.sorting.set(null);
        this.filters.set({});
        this.expanded.set(new Set());
        this.isCreating.set(false);
        this.createModel.set({});
        // Fresh relations on (re)entry; rows themselves reload via the effect.
        this.relationCache.clear(m.name);
      }
    });

    // (Re)load the first page whenever the table, sort or filter changes.
    effect(() => {
      const m = this.meta();
      // Track dependencies explicitly so the effect re-runs on change.
      this.order();
      this.filterValue();
      if (!m) return;
      void this.loadFirstPage();
    });
  }

  // ---- Data loading ----

  private async fetchPage(pageParam: string | number | null): Promise<{
    rows: Row[];
    total: number;
    endCursor: string | null;
    hasNextPage: boolean;
  }> {
    const meta = this.meta()!;
    const listQuery = buildListQuery(meta);
    const vars: Record<string, unknown> = { order: this.order(), filter: this.filterValue() };
    if (meta.cursor) {
      vars['first'] = PAGE_SIZE;
      vars['after'] = pageParam ?? null;
    } else {
      vars['limit'] = PAGE_SIZE;
      vars['offset'] = typeof pageParam === 'number' ? pageParam : 0;
    }
    const result = await this.gql.request<Record<string, Record<string, unknown>>>(listQuery, vars);
    const window = result[meta.listField] ?? {};
    const pageRows = (window[meta.nodesField] as Row[]) ?? [];
    const total = (window[meta.totalCountField] as number) ?? 0;
    if (meta.cursor) {
      const pi = window[meta.cursor.pageInfoField] as Record<string, unknown> | undefined;
      return {
        rows: pageRows,
        total,
        endCursor: (pi?.[meta.cursor.endCursorField] as string | null) ?? null,
        hasNextPage: Boolean(pi?.[meta.cursor.hasNextPageField]),
      };
    }
    return { rows: pageRows, total, endCursor: null, hasNextPage: pageRows.length === PAGE_SIZE };
  }

  private async loadFirstPage(): Promise<void> {
    const token = ++this.loadToken;
    this.isFetching.set(true);
    this.loadError.set(null);
    try {
      const page = await this.fetchPage(null);
      if (token !== this.loadToken) return; // superseded by a newer load
      this.rows.set(page.rows);
      this.total.set(page.total);
      this.endCursor = page.endCursor;
      this.offset = page.rows.length;
      this.hasNextPage.set(page.hasNextPage);
    } catch (e) {
      if (token === this.loadToken) this.loadError.set(errorMessage(e));
    } finally {
      if (token === this.loadToken) this.isFetching.set(false);
    }
  }

  protected async loadNextPage(): Promise<void> {
    const meta = this.meta();
    if (!meta || !this.hasNextPage() || this.isFetchingNext()) return;
    const token = this.loadToken;
    this.isFetchingNext.set(true);
    try {
      const pageParam = meta.cursor ? this.endCursor : this.offset;
      const page = await this.fetchPage(pageParam);
      if (token !== this.loadToken) return;
      this.rows.update((prev) => [...prev, ...page.rows]);
      this.total.set(page.total);
      this.endCursor = page.endCursor;
      this.offset += page.rows.length;
      this.hasNextPage.set(page.hasNextPage);
    } catch (e) {
      if (token === this.loadToken) this.loadError.set(errorMessage(e));
    } finally {
      if (token === this.loadToken) this.isFetchingNext.set(false);
    }
  }

  private reload(): void {
    // A reload follows every mutation — drop cached relation details for this
    // table so expanded rows re-fetch fresh related records.
    this.relationCache.clear(this.meta()?.name);
    void this.loadFirstPage();
  }

  // ---- Filters / sort ----

  protected filterColumn(name: string) {
    return this.meta()?.filter?.columns.get(name);
  }

  protected canSort(col: ColumnMeta): boolean {
    return this.meta()?.order?.sortableColumns.has(col.name) ?? false;
  }

  protected header(col: ColumnMeta): string {
    return humanizeColumn(col.name) + (col.isPk ? ' 🔑' : '');
  }

  protected toggleSort(id: string): void {
    this.sorting.update((s) => {
      if (!s || s.id !== id) return { id, desc: false };
      if (!s.desc) return { id, desc: true };
      return null;
    });
  }

  protected applyFilter(col: string, filter: ActiveFilter): void {
    this.filters.update((prev) => ({ ...prev, [col]: filter }));
  }

  protected clearFilter(col: string): void {
    this.filters.update((prev) => {
      const next = { ...prev };
      delete next[col];
      return next;
    });
  }

  protected clearFilters(): void {
    this.filters.set({});
  }

  // ---- Expansion ----

  protected rowTrack(row: Row): string {
    const m = this.meta()!;
    return m.pkColumns.length ? rowKey(m, row) : JSON.stringify(row);
  }

  protected isExpanded(row: Row): boolean {
    return this.canExpand() && this.expanded().has(rowKey(this.meta()!, row));
  }

  protected toggleExpanded(row: Row): void {
    const key = rowKey(this.meta()!, row);
    this.expanded.update((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  // ---- Mutations ----

  protected fmt(value: unknown): string {
    return formatValue(value);
  }

  protected commitEdit(row: Row, column: ColumnMeta, raw: unknown): void {
    const meta = this.meta()!;
    const mutation = buildUpdateMutation(meta);
    if (!mutation || !meta.update) {
      this.mutationError.set('Table is not updatable');
      return;
    }
    let value: unknown;
    try {
      value = coerceValue(column, raw);
    } catch (e) {
      this.mutationError.set(`Invalid value for ${column.name}: ${errorMessage(e)}`);
      return;
    }
    if (value === row[column.name]) return;
    void this.gql
      .request(mutation, {
        ...pkVariables(meta.update.pkArgs, row),
        [meta.update.patchArg.name]: { [column.name]: value },
      })
      .then(() => {
        this.mutationError.set(null);
        this.reload();
      })
      .catch((e) => this.mutationError.set(errorMessage(e)));
  }

  protected toggleCreating(): void {
    this.isCreating.update((v) => !v);
  }

  protected setCreateField(name: string, raw: unknown): void {
    this.createModel.update((m) => ({ ...m, [name]: String(raw ?? '') }));
  }

  protected submitCreate(): void {
    const meta = this.meta()!;
    const mutation = buildCreateMutation(meta);
    if (!mutation || !meta.create) {
      this.mutationError.set('Table is not insertable');
      return;
    }
    // Read the collected values through the Signal Forms field tree.
    const draft = this.createForm().value();
    const input: Record<string, unknown> = {};
    for (const col of meta.columns) {
      if (!col.creatable) continue;
      const raw = draft[col.name];
      if (raw === undefined || raw === '') continue;
      try {
        input[col.name] = coerceValue(col, raw);
      } catch (e) {
        this.mutationError.set(`Invalid value for ${col.name}: ${errorMessage(e)}`);
        return;
      }
    }
    this.createPending.set(true);
    void this.gql
      .request(mutation, { [meta.create.inputArg.name]: input })
      .then(() => {
        this.mutationError.set(null);
        this.createModel.set({});
        this.isCreating.set(false);
        this.reload();
      })
      .catch((e) => this.mutationError.set(errorMessage(e)))
      .finally(() => this.createPending.set(false));
  }

  protected deleteRow(row: Row): void {
    const meta = this.meta()!;
    const mutation = buildDeleteMutation(meta);
    if (!mutation || !meta.remove) {
      this.mutationError.set('Table is not deletable');
      return;
    }
    if (!window.confirm('Delete this row?')) return;
    this.deletePending.set(true);
    void this.gql
      .request(mutation, pkVariables(meta.remove.pkArgs, row))
      .then(() => {
        this.mutationError.set(null);
        this.reload();
      })
      .catch((e) => this.mutationError.set(errorMessage(e)))
      .finally(() => this.deletePending.set(false));
  }
}
