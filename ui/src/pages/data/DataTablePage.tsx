import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  type ColumnDef,
  type SortingState,
  flexRender,
  getCoreRowModel,
  useReactTable,
} from "@tanstack/react-table";
import { useInfiniteQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useParams } from "@tanstack/react-router";

import {
  type ActiveFilter,
  type ColumnMeta,
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
} from "@/lib/dataSchema";
import { GraphQLRequestError, useGql } from "@/lib/graphqlClient";
import { useAuth } from "@/lib/auth";

import { ColumnFilter } from "./ColumnFilter";
import { useDataSchema } from "./useDataSchema";

/** Rows fetched per page (cursor ``first`` / offset ``limit``). */
const PAGE_SIZE = 50;

interface Page {
  rows: Row[];
  total: number;
  endCursor: string | null;
  hasNextPage: boolean;
}

type Row = Record<string, unknown>;

function errorMessage(e: unknown): string {
  if (e instanceof GraphQLRequestError) return e.message;
  if (e instanceof Error) return e.message;
  return String(e);
}

export function DataTablePage() {
  const { table } = useParams({ from: "/data/$table" });
  const { data: schema, isLoading: schemaLoading } = useDataSchema();
  const meta = schema?.tables.find((t) => t.name === table);

  if (schemaLoading) {
    return <Centered>Loading schema…</Centered>;
  }
  if (!schema || !meta) {
    return <Centered>Unknown table “{table}”.</Centered>;
  }
  // Remount per table so internal state (pagination, sorting, draft) resets.
  return <TableView key={meta.name} meta={meta} sortAsc={schema.sortAsc} sortDesc={schema.sortDesc} />;
}

function TableView({
  meta,
  sortAsc,
  sortDesc,
}: {
  meta: TableMeta;
  sortAsc: string;
  sortDesc: string;
}) {
  const gql = useGql();
  const queryClient = useQueryClient();
  const { status } = useAuth();
  const canWrite = status === "authenticated";

  const [sorting, setSorting] = useState<SortingState>([]);
  const [filters, setFilters] = useState<Record<string, ActiveFilter>>({});
  const [mutationError, setMutationError] = useState<string | null>(null);
  const [creating, setCreating] = useState<Record<string, string>>({});
  const [isCreating, setIsCreating] = useState(false);

  const listQuery = useMemo(() => buildListQuery(meta), [meta]);
  const updateMutationStr = useMemo(() => buildUpdateMutation(meta), [meta]);
  const createMutationStr = useMemo(() => buildCreateMutation(meta), [meta]);
  const deleteMutationStr = useMemo(() => buildDeleteMutation(meta), [meta]);

  const colByName = useMemo(
    () => new Map(meta.columns.map((c) => [c.name, c])),
    [meta],
  );

  // Single-column server-side sort, shaped to the schema's nested order input
  // (``[{ field: { col: ASC|DESC } }]``).
  const order = useMemo(() => {
    const sort = sorting[0];
    if (!sort) return null;
    return buildOrderValue(meta, sort.id, sort.desc, sortAsc, sortDesc);
  }, [meta, sorting, sortAsc, sortDesc]);

  const filterValue = useMemo(() => buildFilterValue(meta, filters), [meta, filters]);

  const {
    data,
    error,
    isFetching,
    isFetchingNextPage,
    hasNextPage,
    fetchNextPage,
  } = useInfiniteQuery({
    queryKey: ["data", "rows", meta.name, order, filterValue] as const,
    initialPageParam: null as string | number | null,
    queryFn: async ({ pageParam }): Promise<Page> => {
      const vars: Record<string, unknown> = { order, filter: filterValue };
      if (meta.cursor) {
        vars.first = PAGE_SIZE;
        vars.after = pageParam ?? null;
      } else {
        vars.limit = PAGE_SIZE;
        vars.offset = typeof pageParam === "number" ? pageParam : 0;
      }
      const result = await gql<Record<string, Record<string, unknown>>>(listQuery, vars);
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
    },
    getNextPageParam: (lastPage, allPages) => {
      if (meta.cursor) return lastPage.hasNextPage ? (lastPage.endCursor ?? undefined) : undefined;
      return lastPage.rows.length === PAGE_SIZE ? allPages.length * PAGE_SIZE : undefined;
    },
    placeholderData: (prev) => prev,
  });

  const rows = useMemo(() => data?.pages.flatMap((p) => p.rows) ?? [], [data]);
  const total = data?.pages[0]?.total ?? 0;
  const activeFilterCount = Object.keys(filters).length;

  // Auto-load the next page when the bottom sentinel scrolls into view.
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const sentinelRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    const el = sentinelRef.current;
    if (!el) return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries[0]?.isIntersecting && hasNextPage && !isFetchingNextPage) {
          void fetchNextPage();
        }
      },
      { root: scrollRef.current, rootMargin: "300px" },
    );
    observer.observe(el);
    return () => observer.disconnect();
  }, [hasNextPage, isFetchingNextPage, fetchNextPage]);

  const applyFilter = useCallback((col: string, filter: ActiveFilter) => {
    setFilters((prev) => ({ ...prev, [col]: filter }));
  }, []);
  const clearFilter = useCallback((col: string) => {
    setFilters((prev) => {
      const next = { ...prev };
      delete next[col];
      return next;
    });
  }, []);

  function invalidateRows() {
    void queryClient.invalidateQueries({ queryKey: ["data", "rows", meta.name] });
  }

  const updateMutation = useMutation({
    mutationFn: async (vars: { row: Row; column: ColumnMeta; value: unknown }) => {
      if (!updateMutationStr || !meta.update) throw new Error("Table is not updatable");
      return gql(updateMutationStr, {
        ...pkVariables(meta.update.pkArgs, vars.row),
        [meta.update.patchArg.name]: { [vars.column.name]: vars.value },
      });
    },
    onSuccess: () => {
      setMutationError(null);
      invalidateRows();
    },
    onError: (e) => setMutationError(errorMessage(e)),
  });

  const createMutation = useMutation({
    mutationFn: async (input: Record<string, unknown>) => {
      if (!createMutationStr || !meta.create) throw new Error("Table is not insertable");
      return gql(createMutationStr, { [meta.create.inputArg.name]: input });
    },
    onSuccess: () => {
      setMutationError(null);
      setCreating({});
      setIsCreating(false);
      invalidateRows();
    },
    onError: (e) => setMutationError(errorMessage(e)),
  });

  const deleteMutation = useMutation({
    mutationFn: async (row: Row) => {
      if (!deleteMutationStr || !meta.remove) throw new Error("Table is not deletable");
      return gql(deleteMutationStr, pkVariables(meta.remove.pkArgs, row));
    },
    onSuccess: () => {
      setMutationError(null);
      invalidateRows();
    },
    onError: (e) => setMutationError(errorMessage(e)),
  });

  function commitEdit(row: Row, column: ColumnMeta, raw: unknown) {
    let value: unknown;
    try {
      value = coerceValue(column, raw);
    } catch (e) {
      setMutationError(`Invalid value for ${column.name}: ${errorMessage(e)}`);
      return;
    }
    if (value === row[column.name]) return;
    updateMutation.mutate({ row, column, value });
  }

  function submitCreate() {
    const input: Record<string, unknown> = {};
    for (const col of meta.columns) {
      if (!col.creatable) continue;
      const raw = creating[col.name];
      if (raw === undefined || raw === "") continue;
      try {
        input[col.name] = coerceValue(col, raw);
      } catch (e) {
        setMutationError(`Invalid value for ${col.name}: ${errorMessage(e)}`);
        return;
      }
    }
    createMutation.mutate(input);
  }

  const columns = useMemo<ColumnDef<Row>[]>(() => {
    const cols: ColumnDef<Row>[] = meta.columns.map((col) => ({
      id: col.name,
      accessorKey: col.name,
      header: humanizeColumn(col.name) + (col.isPk ? " 🔑" : ""),
      enableSorting: meta.order?.sortableColumns.has(col.name) ?? false,
      cell: (info) => {
        const editable = canWrite && col.updatable && !col.isPk;
        if (!editable) {
          return <ReadOnly value={info.getValue()} />;
        }
        return (
          <EditableCell
            col={col}
            value={info.getValue()}
            onCommit={(raw) => commitEdit(info.row.original, col, raw)}
          />
        );
      },
    }));
    return cols;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [meta, canWrite]);

  const tableInstance = useReactTable({
    data: rows,
    columns,
    getCoreRowModel: getCoreRowModel(),
    manualSorting: true,
    state: { sorting },
    onSortingChange: setSorting,
  });

  const creatable = meta.columns.filter((c) => c.creatable);

  return (
    <div className="flex h-full flex-col">
      {/* Toolbar */}
      <div className="flex items-center justify-between gap-3 border-b border-zinc-200 px-4 py-2.5 dark:border-zinc-800">
        <div className="min-w-0">
          <h1 className="truncate text-sm font-semibold text-zinc-900 dark:text-zinc-50">
            {meta.label}
          </h1>
          <p className="text-xs text-zinc-500 dark:text-zinc-400">
            {total} row{total === 1 ? "" : "s"}
          </p>
        </div>
        <div className="flex items-center gap-2">
          {isFetching && !isFetchingNextPage && (
            <span className="text-xs text-zinc-400">Loading…</span>
          )}
          {activeFilterCount > 0 && (
            <button
              type="button"
              onClick={() => setFilters({})}
              className="rounded-md border border-zinc-300 px-2.5 py-1 text-xs font-medium text-zinc-700 transition-colors hover:bg-zinc-100 dark:border-zinc-700 dark:text-zinc-200 dark:hover:bg-zinc-800"
            >
              Clear {activeFilterCount} filter{activeFilterCount === 1 ? "" : "s"}
            </button>
          )}
          {meta.create && (
            <button
              type="button"
              disabled={!canWrite}
              onClick={() => setIsCreating((v) => !v)}
              title={canWrite ? "Insert a new row" : "Sign in to modify data"}
              className="rounded-md border border-zinc-300 px-2.5 py-1 text-xs font-medium text-zinc-700 transition-colors hover:bg-zinc-100 disabled:cursor-not-allowed disabled:opacity-50 dark:border-zinc-700 dark:text-zinc-200 dark:hover:bg-zinc-800"
            >
              {isCreating ? "Cancel" : "Add row"}
            </button>
          )}
        </div>
      </div>

      {!canWrite && (
        <div className="border-b border-amber-200 bg-amber-50 px-4 py-2 text-xs text-amber-800 dark:border-amber-900/50 dark:bg-amber-950/40 dark:text-amber-300">
          You are not signed in — rows are read-only. Sign in (top-right badge)
          to modify data.
        </div>
      )}
      {mutationError && (
        <div className="flex items-start justify-between gap-3 border-b border-red-200 bg-red-50 px-4 py-2 text-xs text-red-700 dark:border-red-900/50 dark:bg-red-950/40 dark:text-red-300">
          <span className="break-words">{mutationError}</span>
          <button
            type="button"
            onClick={() => setMutationError(null)}
            className="shrink-0 font-medium underline"
          >
            Dismiss
          </button>
        </div>
      )}
      {error && (
        <div className="border-b border-red-200 bg-red-50 px-4 py-2 text-xs text-red-700 dark:border-red-900/50 dark:bg-red-950/40 dark:text-red-300">
          {errorMessage(error)}
        </div>
      )}

      {/* Table */}
      <div ref={scrollRef} className="min-h-0 flex-1 overflow-auto">
        <table className="w-full border-collapse text-sm">
          <thead className="sticky top-0 z-10 bg-zinc-50 dark:bg-zinc-950">
            {tableInstance.getHeaderGroups().map((hg) => (
              <tr key={hg.id}>
                {hg.headers.map((header) => {
                  const sorted = header.column.getIsSorted();
                  const canSort = header.column.getCanSort();
                  const colMeta = colByName.get(header.column.id);
                  const filterCol = meta.filter?.columns.get(header.column.id);
                  return (
                    <th
                      key={header.id}
                      className="select-none whitespace-nowrap border-b border-zinc-200 px-3 py-2 text-left font-semibold text-zinc-600 dark:border-zinc-800 dark:text-zinc-300"
                    >
                      <span className="inline-flex items-center">
                        <span
                          onClick={canSort ? header.column.getToggleSortingHandler() : undefined}
                          className={canSort ? "cursor-pointer" : ""}
                        >
                          {flexRender(header.column.columnDef.header, header.getContext())}
                          {sorted === "asc" && " ▲"}
                          {sorted === "desc" && " ▼"}
                        </span>
                        {colMeta && filterCol && (
                          <ColumnFilter
                            column={colMeta}
                            filterColumn={filterCol}
                            active={filters[header.column.id]}
                            onApply={(f) => applyFilter(header.column.id, f)}
                            onClear={() => clearFilter(header.column.id)}
                          />
                        )}
                      </span>
                    </th>
                  );
                })}
                {meta.remove && (
                  <th className="border-b border-zinc-200 px-3 py-2 dark:border-zinc-800" />
                )}
              </tr>
            ))}
          </thead>
          <tbody>
            {isCreating && meta.create && (
              <tr className="bg-blue-50/50 dark:bg-blue-950/20">
                {meta.columns.map((col) => (
                  <td key={col.name} className="border-b border-zinc-100 px-3 py-1.5 dark:border-zinc-800">
                    {col.creatable ? (
                      <EditableCell
                        col={col}
                        value={creating[col.name] ?? ""}
                        onCommit={(raw) =>
                          setCreating((c) => ({ ...c, [col.name]: String(raw ?? "") }))
                        }
                        commitOnChange
                        placeholder={col.nullable ? "null" : ""}
                      />
                    ) : (
                      <span className="text-xs text-zinc-400">auto</span>
                    )}
                  </td>
                ))}
                <td className="border-b border-zinc-100 px-3 py-1.5 text-right dark:border-zinc-800">
                  <button
                    type="button"
                    disabled={createMutation.isPending}
                    onClick={submitCreate}
                    className="rounded-md bg-blue-600 px-2.5 py-1 text-xs font-medium text-white transition-colors hover:bg-blue-700 disabled:opacity-50"
                  >
                    Save
                  </button>
                </td>
              </tr>
            )}
            {tableInstance.getRowModel().rows.map((row) => (
              <tr
                key={row.id}
                className="hover:bg-zinc-50 dark:hover:bg-zinc-800/50"
              >
                {row.getVisibleCells().map((cell) => (
                  <td
                    key={cell.id}
                    className="max-w-xs truncate border-b border-zinc-100 px-3 py-1.5 align-top dark:border-zinc-800"
                  >
                    {flexRender(cell.column.columnDef.cell, cell.getContext())}
                  </td>
                ))}
                {meta.remove && (
                  <td className="border-b border-zinc-100 px-3 py-1.5 text-right dark:border-zinc-800">
                    <button
                      type="button"
                      disabled={!canWrite || deleteMutation.isPending}
                      onClick={() => {
                        if (window.confirm("Delete this row?")) {
                          deleteMutation.mutate(row.original);
                        }
                      }}
                      title={canWrite ? "Delete row" : "Sign in to modify data"}
                      className="rounded p-1 text-zinc-400 transition-colors hover:bg-red-50 hover:text-red-600 disabled:cursor-not-allowed disabled:opacity-40 dark:hover:bg-red-950/40"
                    >
                      <TrashIcon />
                    </button>
                  </td>
                )}
              </tr>
            ))}
            {rows.length === 0 && !isFetching && (
              <tr>
                <td
                  colSpan={meta.columns.length + (meta.remove ? 1 : 0)}
                  className="px-3 py-8 text-center text-sm text-zinc-500 dark:text-zinc-400"
                >
                  No rows{activeFilterCount > 0 ? " match the active filters" : ""}.
                </td>
              </tr>
            )}
          </tbody>
        </table>
        {/* Infinite-scroll sentinel: observed to auto-load the next page. */}
        <div ref={sentinelRef} className="h-px w-full" />
        {isFetchingNextPage && (
          <div className="py-3 text-center text-xs text-zinc-400">Loading more…</div>
        )}
      </div>

      {/* Status bar */}
      <div className="flex items-center justify-between gap-3 border-t border-zinc-200 px-4 py-2 text-xs dark:border-zinc-800">
        <div className="flex items-center gap-2 text-zinc-500 dark:text-zinc-400">
          {creatable.length === 0 && <span className="text-zinc-400">(no insertable columns)</span>}
        </div>
        <div className="text-zinc-500 dark:text-zinc-400">
          Loaded {rows.length} of {total} row{total === 1 ? "" : "s"}
          {!hasNextPage && rows.length > 0 && " · end"}
        </div>
      </div>
    </div>
  );
}

function humanizeColumn(name: string): string {
  return name
    .replace(/_/g, " ")
    .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

function ReadOnly({ value }: { value: unknown }) {
  const text = formatValue(value);
  return (
    <span
      className="block truncate text-zinc-600 dark:text-zinc-400"
      title={text}
    >
      {text || <span className="text-zinc-300 dark:text-zinc-600">—</span>}
    </span>
  );
}

const inputClass =
  "w-full rounded border border-transparent bg-transparent px-1 py-0.5 text-zinc-900 outline-none focus:border-blue-400 focus:bg-white dark:text-zinc-100 dark:focus:bg-zinc-900";

function EditableCell({
  col,
  value,
  onCommit,
  commitOnChange = false,
  placeholder,
}: {
  col: ColumnMeta;
  value: unknown;
  onCommit: (raw: unknown) => void;
  commitOnChange?: boolean;
  placeholder?: string;
}) {
  const [draft, setDraft] = useState(() => formatValue(value));
  useEffect(() => {
    setDraft(formatValue(value));
  }, [value]);

  if (col.editor === "boolean") {
    return (
      <input
        type="checkbox"
        checked={value === true || value === "true"}
        onChange={(e) => onCommit(e.target.checked)}
        className="h-4 w-4 accent-blue-600"
      />
    );
  }

  if (col.editor === "enum") {
    return (
      <select
        value={value == null ? "" : String(value)}
        onChange={(e) => onCommit(e.target.value)}
        className={inputClass}
      >
        {col.nullable && <option value="">—</option>}
        {(col.enumValues ?? []).map((v) => (
          <option key={v} value={v}>
            {v}
          </option>
        ))}
      </select>
    );
  }

  const numeric = col.editor === "integer" || col.editor === "number";
  return (
    <input
      type={numeric ? "number" : "text"}
      step={col.editor === "integer" ? 1 : "any"}
      value={draft}
      placeholder={placeholder}
      onChange={(e) => {
        setDraft(e.target.value);
        if (commitOnChange) onCommit(e.target.value);
      }}
      onBlur={() => {
        if (!commitOnChange && draft !== formatValue(value)) onCommit(draft);
      }}
      onKeyDown={(e) => {
        if (e.key === "Enter") (e.target as HTMLInputElement).blur();
      }}
      className={inputClass}
    />
  );
}

function Centered({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex h-full items-center justify-center p-8 text-sm text-zinc-500 dark:text-zinc-400">
      {children}
    </div>
  );
}

function TrashIcon() {
  return (
    <svg
      className="h-4 w-4"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M3 6h18M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2m2 0v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6" />
    </svg>
  );
}
