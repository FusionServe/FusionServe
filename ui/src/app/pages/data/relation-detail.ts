import {
  ChangeDetectionStrategy,
  Component,
  type OnInit,
  computed,
  inject,
  input,
  signal,
} from '@angular/core';
import { RouterLink } from '@angular/router';

import { GraphQLRequestError, GraphqlService } from '../../core/graphql.client';
import {
  type DataSchema,
  type RelationMeta,
  type TableMeta,
  buildDetailQuery,
  displayColumn,
  formatValue,
  pkVariables,
  relationTargetTable,
  rowKey,
} from '../../lib/data-schema';
import { RelationCacheService } from './relation-cache.service';

type Row = Record<string, unknown>;

/** Max related rows rendered inline per to-many relation before truncating. */
const MAX_RELATED_ROWS = 25;

interface Section {
  relation: RelationMeta;
  target: TableMeta | undefined;
  records: Row[];
  shown: Row[];
  displayName: string | undefined;
}

/**
 * Lazy detail panel for a single row's relationships.
 *
 * Fetches the row via the singular primary-key lookup (selecting one level of
 * relations) when instantiated (the parent only renders it when a row is
 * expanded), then renders each relation as a section: a compact, read-only
 * mini-table of the related records (one row for to-one, N for to-many) with
 * links to the related table.
 */
@Component({
  selector: 'app-relation-detail',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink],
  template: `
    @if (loading()) {
      <p class="px-4 py-3 text-xs text-zinc-500 dark:text-zinc-400">Loading related…</p>
    } @else if (error(); as err) {
      <p class="px-4 py-3 text-xs text-red-600 dark:text-red-400">{{ err }}</p>
    } @else {
      <div class="space-y-4 bg-zinc-50/60 px-4 py-3 dark:bg-zinc-950/40">
        @for (section of sections(); track section.relation.name) {
          <section>
            <div class="mb-1.5 flex items-center gap-2">
              <h3 class="text-xs font-semibold uppercase tracking-wide text-zinc-500 dark:text-zinc-400">
                {{ section.relation.label }}
              </h3>
              @if (section.relation.toMany) {
                <span
                  class="rounded-full bg-zinc-200 px-1.5 text-[11px] font-medium text-zinc-600 dark:bg-zinc-700 dark:text-zinc-300"
                >
                  {{ section.records.length }}
                </span>
              }
              @if (section.target; as target) {
                <a
                  [routerLink]="['/data', target.name]"
                  class="text-[11px] font-medium text-blue-600 hover:underline dark:text-blue-400"
                >
                  Open {{ target.label }} →
                </a>
              }
            </div>

            @if (section.records.length === 0) {
              <p class="text-xs text-zinc-400 dark:text-zinc-500">—</p>
            } @else if (section.target; as target) {
              <div class="overflow-x-auto rounded-md border border-zinc-200 dark:border-zinc-800">
                <table class="w-full border-collapse text-xs">
                  <thead class="bg-zinc-100 dark:bg-zinc-900">
                    <tr>
                      @for (c of target.columns; track c.name) {
                        <th
                          class="whitespace-nowrap border-b border-zinc-200 px-2.5 py-1.5 text-left font-semibold text-zinc-600 dark:border-zinc-800 dark:text-zinc-300"
                        >
                          {{ c.name }}
                        </th>
                      }
                    </tr>
                  </thead>
                  <tbody>
                    @for (record of section.shown; track $index) {
                      <tr class="hover:bg-zinc-100/60 dark:hover:bg-zinc-800/40">
                        @for (c of target.columns; track c.name) {
                          <td
                            class="max-w-[16rem] truncate border-b border-zinc-100 px-2.5 py-1 align-top dark:border-zinc-800/60"
                            [title]="fmt(record[c.name])"
                          >
                            @if (section.displayName === c.name) {
                              <a
                                [routerLink]="['/data', target.name]"
                                class="text-blue-600 hover:underline dark:text-blue-400"
                              >
                                {{ fmt(record[c.name]) || '—' }}
                              </a>
                            } @else {
                              {{ fmt(record[c.name]) || '—' }}
                            }
                          </td>
                        }
                      </tr>
                    }
                  </tbody>
                </table>
                @if (section.records.length > section.shown.length) {
                  <div
                    class="border-t border-zinc-200 px-2.5 py-1 text-[11px] text-zinc-500 dark:border-zinc-800 dark:text-zinc-400"
                  >
                    +{{ section.records.length - section.shown.length }} more —
                    <a
                      [routerLink]="['/data', target.name]"
                      class="font-medium text-blue-600 hover:underline dark:text-blue-400"
                    >
                      open {{ target.label }}
                    </a>
                  </div>
                }
              </div>
            } @else {
              <pre
                class="overflow-x-auto rounded-md border border-zinc-200 bg-white p-2 text-xs text-zinc-600 dark:border-zinc-800 dark:bg-zinc-900 dark:text-zinc-300"
                >{{ stringify(section.records) }}</pre
              >
            }
          </section>
        }
      </div>
    }
  `,
})
export class RelationDetail implements OnInit {
  private readonly gql = inject(GraphqlService);
  private readonly cache = inject(RelationCacheService);

  readonly meta = input.required<TableMeta>();
  readonly schema = input.required<DataSchema>();
  readonly row = input.required<Row>();

  protected readonly loading = signal(true);
  protected readonly error = signal<string | null>(null);
  private readonly data = signal<Record<string, unknown>>({});

  protected readonly sections = computed<Section[]>(() => {
    const meta = this.meta();
    const schema = this.schema();
    const data = this.data();
    return meta.relations.map((relation) => {
      const value = data[relation.name] ?? null;
      const target = relationTargetTable(schema, relation);
      const records: Row[] = Array.isArray(value)
        ? (value as Row[])
        : value
          ? [value as Row]
          : [];
      return {
        relation,
        target,
        records,
        shown: records.slice(0, MAX_RELATED_ROWS),
        displayName: target ? displayColumn(target)?.name : undefined,
      };
    });
  });

  async ngOnInit(): Promise<void> {
    const meta = this.meta();
    const detailQuery = buildDetailQuery(meta, this.schema());
    if (!detailQuery || !meta.pkLookup) {
      this.loading.set(false);
      return;
    }
    // Serve from the session cache first (instant re-expand); the cache is
    // cleared by the grid on every mutation reload so this is never stale.
    const key = `${meta.name}:${rowKey(meta, this.row())}`;
    const cached = this.cache.get(key);
    if (cached) {
      this.data.set(cached);
      this.loading.set(false);
      return;
    }
    try {
      const variables = pkVariables(meta.pkLookup.pkArgs, this.row());
      const result = await this.gql.request<Record<string, Row | null>>(detailQuery, variables);
      const detail = result[meta.pkLookup.field] ?? {};
      this.cache.set(key, detail);
      this.data.set(detail);
      this.error.set(null);
    } catch (e) {
      this.error.set(this.errorMessage(e));
    } finally {
      this.loading.set(false);
    }
  }

  protected fmt(value: unknown): string {
    return formatValue(value);
  }

  protected stringify(records: Row[]): string {
    return JSON.stringify(records, null, 2);
  }

  private errorMessage(e: unknown): string {
    if (e instanceof GraphQLRequestError) return e.message;
    if (e instanceof Error) return e.message;
    return String(e);
  }
}
