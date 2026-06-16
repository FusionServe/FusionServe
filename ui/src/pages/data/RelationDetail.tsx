import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";

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
} from "@/lib/dataSchema";
import { GraphQLRequestError, useGql } from "@/lib/graphqlClient";

type Row = Record<string, unknown>;

/** Max related rows rendered inline per to-many relation before truncating. */
const MAX_RELATED_ROWS = 25;

function errorMessage(e: unknown): string {
  if (e instanceof GraphQLRequestError) return e.message;
  if (e instanceof Error) return e.message;
  return String(e);
}

/**
 * Lazy detail panel for a single row's relationships.
 *
 * Fetches the row via the singular primary-key lookup (selecting one level of
 * relations) only when expanded, then renders each relation as a section: a
 * compact, read-only mini-table of the related records (one row for to-one, N
 * for to-many) with links to the related table.
 */
export function RelationDetail({
  meta,
  schema,
  row,
}: {
  meta: TableMeta;
  schema: DataSchema;
  row: Row;
}) {
  const gql = useGql();
  const detailQuery = useMemo(() => buildDetailQuery(meta, schema), [meta, schema]);
  const variables = useMemo(
    () => (meta.pkLookup ? pkVariables(meta.pkLookup.pkArgs, row) : {}),
    [meta, row],
  );

  const { data, isLoading, error } = useQuery({
    queryKey: ["data", "detail", meta.name, rowKey(meta, row)] as const,
    enabled: detailQuery !== null,
    queryFn: async () => {
      const result = await gql<Record<string, Row | null>>(detailQuery!, variables);
      return (meta.pkLookup && result[meta.pkLookup.field]) || {};
    },
  });

  if (!detailQuery) return null;

  if (isLoading) {
    return <p className="px-4 py-3 text-xs text-zinc-500 dark:text-zinc-400">Loading related…</p>;
  }
  if (error) {
    return (
      <p className="px-4 py-3 text-xs text-red-600 dark:text-red-400">{errorMessage(error)}</p>
    );
  }

  return (
    <div className="space-y-4 bg-zinc-50/60 px-4 py-3 dark:bg-zinc-950/40">
      {meta.relations.map((rel) => (
        <RelationSection
          key={rel.name}
          relation={rel}
          schema={schema}
          value={data?.[rel.name] ?? null}
        />
      ))}
    </div>
  );
}

function RelationSection({
  relation,
  schema,
  value,
}: {
  relation: RelationMeta;
  schema: DataSchema;
  value: unknown;
}) {
  const target = relationTargetTable(schema, relation);
  const records: Row[] = Array.isArray(value) ? (value as Row[]) : value ? [value as Row] : [];
  const shown = records.slice(0, MAX_RELATED_ROWS);
  const display = target ? displayColumn(target) : undefined;

  return (
    <section>
      <div className="mb-1.5 flex items-center gap-2">
        <h3 className="text-xs font-semibold uppercase tracking-wide text-zinc-500 dark:text-zinc-400">
          {relation.label}
        </h3>
        {relation.toMany && (
          <span className="rounded-full bg-zinc-200 px-1.5 text-[11px] font-medium text-zinc-600 dark:bg-zinc-700 dark:text-zinc-300">
            {records.length}
          </span>
        )}
        {target && (
          <Link
            to="/data/$table"
            params={{ table: target.name }}
            className="text-[11px] font-medium text-blue-600 hover:underline dark:text-blue-400"
          >
            Open {target.label} →
          </Link>
        )}
      </div>

      {records.length === 0 ? (
        <p className="text-xs text-zinc-400 dark:text-zinc-500">—</p>
      ) : target ? (
        <div className="overflow-x-auto rounded-md border border-zinc-200 dark:border-zinc-800">
          <table className="w-full border-collapse text-xs">
            <thead className="bg-zinc-100 dark:bg-zinc-900">
              <tr>
                {target.columns.map((c) => (
                  <th
                    key={c.name}
                    className="whitespace-nowrap border-b border-zinc-200 px-2.5 py-1.5 text-left font-semibold text-zinc-600 dark:border-zinc-800 dark:text-zinc-300"
                  >
                    {c.name}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {shown.map((record, i) => (
                <tr key={i} className="hover:bg-zinc-100/60 dark:hover:bg-zinc-800/40">
                  {target.columns.map((c) => {
                    const text = formatValue(record[c.name]);
                    const isDisplay = display?.name === c.name;
                    return (
                      <td
                        key={c.name}
                        className="max-w-[16rem] truncate border-b border-zinc-100 px-2.5 py-1 align-top dark:border-zinc-800/60"
                        title={text}
                      >
                        {isDisplay ? (
                          <Link
                            to="/data/$table"
                            params={{ table: target.name }}
                            className="text-blue-600 hover:underline dark:text-blue-400"
                          >
                            {text || "—"}
                          </Link>
                        ) : (
                          text || <span className="text-zinc-300 dark:text-zinc-600">—</span>
                        )}
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
          {records.length > shown.length && (
            <div className="border-t border-zinc-200 px-2.5 py-1 text-[11px] text-zinc-500 dark:border-zinc-800 dark:text-zinc-400">
              +{records.length - shown.length} more —{" "}
              <Link
                to="/data/$table"
                params={{ table: target.name }}
                className="font-medium text-blue-600 hover:underline dark:text-blue-400"
              >
                open {target.label}
              </Link>
            </div>
          )}
        </div>
      ) : (
        // Target table isn't separately listed: fall back to raw JSON.
        <pre className="overflow-x-auto rounded-md border border-zinc-200 bg-white p-2 text-xs text-zinc-600 dark:border-zinc-800 dark:bg-zinc-900 dark:text-zinc-300">
          {JSON.stringify(records, null, 2)}
        </pre>
      )}
    </section>
  );
}
