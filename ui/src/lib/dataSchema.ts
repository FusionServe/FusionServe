import { getIntrospectionQuery } from "graphql";

/**
 * Introspection-driven discovery of the editable table surface.
 *
 * The FusionServe GraphQL schema is generated at runtime from PostgreSQL,
 * so the SPA learns its shape via a standard introspection query rather
 * than codegen. The convention (see ``fusionserve.graphql.build`` and
 * ``fusionserve.connections``):
 *
 *   - Each database table is exposed as a Query field returning a
 *     ``<Row>Connection`` object. The connection carries a flat ``nodes``
 *     list and ``totalCount`` (alongside relay ``edges``/``pageInfo``);
 *     these connection fields are how we enumerate the tables for the nav.
 *   - The connection's ``nodes`` element type is the row type; its
 *     scalar/enum fields are columns, object/list fields are relationships.
 *   - The list field accepts ``limit``/``offset`` and an
 *     ``order: [{ field: { <col>: ASC|DESC } }]`` argument.
 *   - Mutations ``create<Row>`` / ``update<Row>`` / ``delete<Row>`` carry
 *     the input/patch types (via the ``input`` / ``patch`` arguments) and
 *     primary-key arguments, from which we derive creatable/updatable
 *     column sets and the PK columns.
 *
 * All names are read from introspection (never hardcoded) so the code is
 * agnostic to the schema's field-casing configuration.
 */

// ---- Introspection result shapes (minimal subset we consume) ----

type TypeKind =
  | "SCALAR"
  | "OBJECT"
  | "INTERFACE"
  | "UNION"
  | "ENUM"
  | "INPUT_OBJECT"
  | "LIST"
  | "NON_NULL";

interface TypeRef {
  kind: TypeKind;
  name: string | null;
  ofType: TypeRef | null;
}

interface InputValue {
  name: string;
  type: TypeRef;
}

interface FieldDef {
  name: string;
  args: InputValue[];
  type: TypeRef;
}

interface TypeDef {
  kind: TypeKind;
  name: string | null;
  fields: FieldDef[] | null;
  inputFields: InputValue[] | null;
  enumValues: { name: string }[] | null;
}

interface SchemaDef {
  queryType: { name: string } | null;
  mutationType: { name: string } | null;
  types: TypeDef[];
}

export interface IntrospectionResult {
  __schema: SchemaDef;
}

/** The standard introspection query string. */
export const INTROSPECTION_QUERY = getIntrospectionQuery();

// ---- Discovered metadata ----

export type EditorKind =
  | "text"
  | "number"
  | "integer"
  | "boolean"
  | "datetime"
  | "date"
  | "json"
  | "enum";

export interface ColumnMeta {
  /** GraphQL field name on the row type. */
  name: string;
  /** Named GraphQL type of the column (e.g. "Int", "UUID"). */
  scalarType: string;
  editor: EditorKind;
  enumValues?: string[];
  nullable: boolean;
  isPk: boolean;
  creatable: boolean;
  updatable: boolean;
}

interface ArgMeta {
  name: string;
  /** SDL rendering of the argument type, e.g. "UUID!" or "OrdersPatch!". */
  sdl: string;
}

export interface TableMeta {
  /** Stable key used in the route and nav (the plural list-field name). */
  name: string;
  /** Humanized label for display. */
  label: string;
  listField: string;
  rowType: string;
  nodesField: string;
  totalCountField: string;
  limitArg: string | null;
  offsetArg: string | null;
  /**
   * The list field's ``order`` argument, when present.
   *
   * The schema's order input is a list whose element nests column directions
   * under a ``field`` sub-input, e.g. ``order: [{ field: { id: ASC } }]``.
   * ``argName`` is the list arg, ``elementSdl`` its SDL (for the variable
   * declaration), ``fieldKey`` the sub-input field name (usually ``field``),
   * and ``sortableColumns`` the set of columns that sub-input accepts.
   */
  order: {
    argName: string;
    elementSdl: string;
    fieldKey: string;
    sortableColumns: Set<string>;
  } | null;
  /** Scalar/enum columns (relationships excluded). */
  columns: ColumnMeta[];
  pkColumns: string[];
  create: { field: string; inputArg: ArgMeta } | null;
  update: { field: string; patchArg: ArgMeta; pkArgs: ArgMeta[] } | null;
  remove: { field: string; pkArgs: ArgMeta[] } | null;
}

export interface DataSchema {
  tables: TableMeta[];
  sortAsc: string;
  sortDesc: string;
}

// ---- Type-ref helpers ----

function namedRef(ref: TypeRef): TypeRef {
  let current = ref;
  while ((current.kind === "NON_NULL" || current.kind === "LIST") && current.ofType) {
    current = current.ofType;
  }
  return current;
}

function typeRefToSDL(ref: TypeRef): string {
  if (ref.kind === "NON_NULL" && ref.ofType) return `${typeRefToSDL(ref.ofType)}!`;
  if (ref.kind === "LIST" && ref.ofType) return `[${typeRefToSDL(ref.ofType)}]`;
  return ref.name ?? "Unknown";
}

function humanize(name: string): string {
  return name
    .replace(/_/g, " ")
    .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
    .replace(/\s+/g, " ")
    .trim()
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

function editorForColumn(named: TypeRef): EditorKind {
  if (named.kind === "ENUM") return "enum";
  switch (named.name) {
    case "Int":
      return "integer";
    case "Float":
    case "Decimal":
    case "BigDecimal":
      return "number";
    case "Boolean":
      return "boolean";
    case "DateTime":
    case "NaiveDateTime":
    case "Timestamp":
      return "datetime";
    case "Date":
      return "date";
    case "JSON":
    case "JSONB":
      return "json";
    default:
      return "text";
  }
}

// ---- Discovery ----

export function discoverSchema(result: IntrospectionResult): DataSchema {
  const schema = result.__schema;
  const typeMap = new Map<string, TypeDef>();
  for (const t of schema.types) {
    if (t.name) typeMap.set(t.name, t);
  }

  const queryType = schema.queryType ? typeMap.get(schema.queryType.name) : undefined;
  const mutationType = schema.mutationType
    ? typeMap.get(schema.mutationType.name)
    : undefined;
  const mutationFields = new Map<string, FieldDef>();
  for (const f of mutationType?.fields ?? []) {
    mutationFields.set(f.name, f);
  }

  const sort = discoverSortDirection(typeMap);
  const tables: TableMeta[] = [];

  for (const field of queryType?.fields ?? []) {
    const connNamed = namedRef(field.type);
    if (connNamed.kind !== "OBJECT" || !connNamed.name?.endsWith("Connection")) {
      continue;
    }
    const connType = typeMap.get(connNamed.name);
    if (!connType?.fields) continue;

    // The connection exposes a flat ``nodes`` list and ``totalCount`` (plus
    // relay ``edges``/``pageInfo``). Prefer the conventional field names, but
    // fall back to shape: ``nodes`` is the list-of-OBJECT whose element type
    // is not an ``*Edge``; ``totalCount`` is the scalar field.
    const nodesFieldDef =
      connType.fields.find((f) => f.name === "nodes") ??
      connType.fields.find((f) => {
        const n = namedRef(f.type);
        return n.kind === "OBJECT" && !n.name?.endsWith("Edge");
      });
    const totalCountFieldDef =
      connType.fields.find((f) => f.name === "totalCount") ??
      connType.fields.find((f) => namedRef(f.type).kind === "SCALAR");
    if (!nodesFieldDef || !totalCountFieldDef) continue;
    const rowTypeName = namedRef(nodesFieldDef.type).name;
    if (!rowTypeName) continue;
    const rowType = typeMap.get(rowTypeName);
    if (!rowType?.fields) continue;

    // Mutations (by convention on the row type name).
    const createDef = mutationFields.get(`create${rowTypeName}`);
    const updateDef = mutationFields.get(`update${rowTypeName}`);
    const deleteDef = mutationFields.get(`delete${rowTypeName}`);

    const update = buildUpdateMeta(updateDef);
    const create = buildCreateMeta(createDef);
    const remove = buildDeleteMeta(deleteDef);

    const pkNames = new Set(
      (update?.pkArgs ?? remove?.pkArgs ?? []).map((a) => a.name),
    );
    const inputFieldNames = create
      ? inputFieldNameSet(typeMap, create.inputArg)
      : new Set<string>();
    const patchFieldNames = update
      ? inputFieldNameSet(typeMap, update.patchArg)
      : new Set<string>();

    // Columns: scalar/enum row fields only (relationships excluded).
    const columns: ColumnMeta[] = [];
    for (const f of rowType.fields) {
      const named = namedRef(f.type);
      if (named.kind !== "SCALAR" && named.kind !== "ENUM") continue;
      columns.push({
        name: f.name,
        scalarType: named.name ?? "Unknown",
        editor: editorForColumn(named),
        enumValues:
          named.kind === "ENUM" && named.name
            ? (typeMap.get(named.name)?.enumValues ?? []).map((e) => e.name)
            : undefined,
        nullable: f.type.kind !== "NON_NULL",
        isPk: pkNames.has(f.name),
        creatable: inputFieldNames.has(f.name),
        updatable: patchFieldNames.has(f.name),
      });
    }

    // Pagination args by name (the backend exposes literal limit/offset).
    const limitArg = field.args.find((a) => a.name.toLowerCase() === "limit");
    const offsetArg = field.args.find((a) => a.name.toLowerCase() === "offset");
    const order = buildOrderMeta(typeMap, field, columns);

    tables.push({
      name: field.name,
      label: humanize(field.name),
      listField: field.name,
      rowType: rowTypeName,
      nodesField: nodesFieldDef.name,
      totalCountField: totalCountFieldDef.name,
      limitArg: limitArg?.name ?? null,
      offsetArg: offsetArg?.name ?? null,
      order,
      columns,
      pkColumns: [...pkNames],
      create,
      update,
      remove,
    });
  }

  tables.sort((a, b) => a.label.localeCompare(b.label));
  return { tables, sortAsc: sort.asc, sortDesc: sort.desc };
}

function buildUpdateMeta(def: FieldDef | undefined): TableMeta["update"] {
  if (!def) return null;
  // Detect the patch arg by name (resilient to the input type's name, which
  // strawberry_orm derives independently); the remaining args are the PK(s).
  const patch =
    def.args.find((a) => a.name === "patch") ??
    def.args.find((a) => namedRef(a.type).kind === "INPUT_OBJECT");
  if (!patch) return null;
  const pkArgs = def.args
    .filter((a) => a.name !== patch.name)
    .map((a) => ({ name: a.name, sdl: typeRefToSDL(a.type) }));
  return {
    field: def.name,
    patchArg: { name: patch.name, sdl: typeRefToSDL(patch.type) },
    pkArgs,
  };
}

function buildCreateMeta(def: FieldDef | undefined): TableMeta["create"] {
  if (!def) return null;
  // The single-row create takes one INPUT_OBJECT argument (named ``input``).
  const input =
    def.args.find((a) => a.name === "input") ??
    def.args.find((a) => namedRef(a.type).kind === "INPUT_OBJECT");
  if (!input) return null;
  return { field: def.name, inputArg: { name: input.name, sdl: typeRefToSDL(input.type) } };
}

/**
 * Discover the list field's ``order`` argument.
 *
 * The schema's order input is ``[<Order>!]`` where ``<Order>`` nests column
 * directions under a ``field`` sub-input: ``order: [{ field: { id: ASC } }]``.
 * Returns the arg/element/sub-field names plus the set of sortable columns.
 */
function buildOrderMeta(
  typeMap: Map<string, TypeDef>,
  field: FieldDef,
  columns: ColumnMeta[],
): TableMeta["order"] {
  // The order arg is a LIST whose element is an INPUT_OBJECT.
  const arg = field.args.find((a) => {
    const inner = a.type.kind === "NON_NULL" ? a.type.ofType : a.type;
    return inner?.kind === "LIST" && namedRef(a.type).kind === "INPUT_OBJECT";
  });
  if (!arg) return null;
  const elementName = namedRef(arg.type).name;
  const elementDef = elementName ? typeMap.get(elementName) : undefined;
  if (!elementDef?.inputFields?.length) return null;

  // The sub-input ("field") whose own fields include our columns.
  const columnNames = new Set(columns.map((c) => c.name));
  let fieldKey: string | null = null;
  let sortableColumns = new Set<string>();
  for (const sub of elementDef.inputFields) {
    const subNamed = namedRef(sub.type);
    if (subNamed.kind !== "INPUT_OBJECT" || !subNamed.name) continue;
    const subDef = typeMap.get(subNamed.name);
    const subFieldNames = (subDef?.inputFields ?? []).map((f) => f.name);
    const matching = subFieldNames.filter((n) => columnNames.has(n));
    if (matching.length > 0) {
      fieldKey = sub.name;
      sortableColumns = new Set(matching);
      break;
    }
  }
  if (!fieldKey) return null;
  // Element SDL without the list/non-null wrappers we re-add at call sites.
  return {
    argName: arg.name,
    elementSdl: elementName ?? "Unknown",
    fieldKey,
    sortableColumns,
  };
}

function buildDeleteMeta(def: FieldDef | undefined): TableMeta["remove"] {
  if (!def) return null;
  return {
    field: def.name,
    pkArgs: def.args.map((a) => ({ name: a.name, sdl: typeRefToSDL(a.type) })),
  };
}

function inputFieldNameSet(
  typeMap: Map<string, TypeDef>,
  arg: ArgMeta,
): Set<string> {
  // Recover the named INPUT_OBJECT from the SDL (strip wrapping !/[]).
  const named = arg.sdl.replace(/[![\]]/g, "");
  const def = typeMap.get(named);
  return new Set((def?.inputFields ?? []).map((f) => f.name));
}

function discoverSortDirection(
  typeMap: Map<string, TypeDef>,
): { asc: string; desc: string } {
  // Find the sort-direction enum: an ENUM with ASC- and DESC-like values.
  for (const def of typeMap.values()) {
    if (def.kind !== "ENUM" || !def.enumValues?.length) continue;
    const asc = def.enumValues.find((v) => v.name.toLowerCase().includes("asc"))?.name;
    const desc = def.enumValues.find((v) => v.name.toLowerCase().includes("desc"))?.name;
    if (asc && desc) return { asc, desc };
  }
  return { asc: "ASC", desc: "DESC" };
}

// ---- Query / mutation string builders ----

function selectionColumns(meta: TableMeta): string {
  return meta.columns.map((c) => c.name).join(" ");
}

/** A single order entry, e.g. ``{ field: { id: ASC } }``. */
export type OrderValue = Record<string, Record<string, string>>;

export function buildListQuery(meta: TableMeta): string {
  const varDefs: string[] = [];
  const args: string[] = [];
  if (meta.limitArg) {
    varDefs.push("$limit: Int");
    args.push(`${meta.limitArg}: $limit`);
  }
  if (meta.offsetArg) {
    varDefs.push("$offset: Int");
    args.push(`${meta.offsetArg}: $offset`);
  }
  if (meta.order) {
    varDefs.push(`$order: [${meta.order.elementSdl}!]`);
    args.push(`${meta.order.argName}: $order`);
  }
  const head = varDefs.length ? `(${varDefs.join(", ")})` : "";
  const argStr = args.length ? `(${args.join(", ")})` : "";
  return `query DataList${head} {
  ${meta.listField}${argStr} {
    ${meta.totalCountField}
    ${meta.nodesField} { __typename ${selectionColumns(meta)} }
  }
}`;
}

/**
 * Build the ``order`` variable value for a single-column sort.
 *
 * Produces the nested list shape the schema expects, e.g.
 * ``[{ field: { id: ASC } }]``. Returns ``null`` when the table has no order
 * argument or the column isn't sortable.
 */
export function buildOrderValue(
  meta: TableMeta,
  columnName: string,
  desc: boolean,
  sortAsc: string,
  sortDesc: string,
): OrderValue[] | null {
  if (!meta.order || !meta.order.sortableColumns.has(columnName)) return null;
  return [{ [meta.order.fieldKey]: { [columnName]: desc ? sortDesc : sortAsc } }];
}

export function buildUpdateMutation(meta: TableMeta): string | null {
  if (!meta.update) return null;
  const { field, patchArg, pkArgs } = meta.update;
  const varDefs = [
    ...pkArgs.map((a) => `$${a.name}: ${a.sdl}`),
    `$${patchArg.name}: ${patchArg.sdl}`,
  ];
  const callArgs = [
    ...pkArgs.map((a) => `${a.name}: $${a.name}`),
    `${patchArg.name}: $${patchArg.name}`,
  ];
  return `mutation DataUpdate(${varDefs.join(", ")}) {
  ${field}(${callArgs.join(", ")}) { __typename ${selectionColumns(meta)} }
}`;
}

export function buildCreateMutation(meta: TableMeta): string | null {
  if (!meta.create) return null;
  const { field, inputArg } = meta.create;
  return `mutation DataCreate($${inputArg.name}: ${inputArg.sdl}) {
  ${field}(${inputArg.name}: $${inputArg.name}) { __typename ${selectionColumns(meta)} }
}`;
}

export function buildDeleteMutation(meta: TableMeta): string | null {
  if (!meta.remove) return null;
  const { field, pkArgs } = meta.remove;
  const varDefs = pkArgs.map((a) => `$${a.name}: ${a.sdl}`);
  const callArgs = pkArgs.map((a) => `${a.name}: $${a.name}`);
  const pkSelection = meta.pkColumns.join(" ") || "__typename";
  return `mutation DataDelete(${varDefs.join(", ")}) {
  ${field}(${callArgs.join(", ")}) { ${pkSelection} }
}`;
}

/** Build the variables object identifying a row by its primary key(s). */
export function pkVariables(
  pkArgs: ArgMeta[],
  row: Record<string, unknown>,
): Record<string, unknown> {
  const vars: Record<string, unknown> = {};
  for (const a of pkArgs) {
    vars[a.name] = row[a.name];
  }
  return vars;
}

/**
 * Coerce a raw editor value to the JSON type expected for a GraphQL variable.
 *
 * @throws {SyntaxError} for invalid JSON in a ``json`` column.
 */
export function coerceValue(column: ColumnMeta, raw: unknown): unknown {
  if (raw === "" || raw === null || raw === undefined) {
    return column.nullable ? null : raw === "" ? null : raw;
  }
  switch (column.editor) {
    case "integer": {
      const n = Number.parseInt(String(raw), 10);
      return Number.isNaN(n) ? null : n;
    }
    case "number": {
      const n = Number.parseFloat(String(raw));
      return Number.isNaN(n) ? null : n;
    }
    case "boolean":
      return Boolean(raw);
    case "json":
      return typeof raw === "string" ? JSON.parse(raw) : raw;
    default:
      return raw;
  }
}

/** Render a stored cell value for display in a read-only or text context. */
export function formatValue(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}
