import { getIntrospectionQuery } from 'graphql';

/**
 * Introspection-driven discovery of the editable table surface.
 *
 * The FusionServe GraphQL schema is generated at runtime from PostgreSQL, so
 * the SPA learns its shape via a standard introspection query rather than
 * codegen. The convention (see ``fusionserve.graphql.build`` and
 * ``fusionserve.connections``):
 *
 *   - Each database table is exposed as a Query field returning a
 *     ``<Row>Connection`` object. The connection carries a flat ``nodes`` list
 *     and ``totalCount`` (alongside relay ``edges``/``pageInfo``); these
 *     connection fields are how we enumerate the tables for the nav.
 *   - The connection's ``nodes`` element type is the row type; its scalar/enum
 *     fields are columns, object/list fields are relationships.
 *   - The list field accepts ``limit``/``offset`` and an
 *     ``order: [{ field: { <col>: ASC|DESC } }]`` argument.
 *   - Mutations ``create<Row>`` / ``update<Row>`` / ``delete<Row>`` carry the
 *     input/patch types (via the ``input`` / ``patch`` arguments) and
 *     primary-key arguments, from which we derive creatable/updatable column
 *     sets and the PK columns.
 *
 * All names are read from introspection (never hardcoded) so the code is
 * agnostic to the schema's field-casing configuration.
 */

// ---- Introspection result shapes (minimal subset we consume) ----

type TypeKind =
  | 'SCALAR'
  | 'OBJECT'
  | 'INTERFACE'
  | 'UNION'
  | 'ENUM'
  | 'INPUT_OBJECT'
  | 'LIST'
  | 'NON_NULL';

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
  | 'text'
  | 'number'
  | 'integer'
  | 'boolean'
  | 'datetime'
  | 'date'
  | 'json'
  | 'enum';

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

/** The "shape" of value a filter operator expects. */
export type FilterValueKind = 'scalar' | 'list' | 'boolean' | 'range';

export interface OperatorMeta {
  /** GraphQL operator field name, e.g. "exact", "iContains", "isNull". */
  name: string;
  valueKind: FilterValueKind;
  /** Named scalar type the operator's value(s) coerce to, e.g. "Int", "String". */
  scalarType: string;
}

export interface FilterColumnMeta {
  /** Column (row-type field) name. */
  name: string;
  operators: OperatorMeta[];
}

/**
 * Cursor (keyset) pagination metadata for a table's list field.
 *
 * The connection field accepts ``first``/``after`` and exposes ``pageInfo``
 * with ``hasNextPage``/``endCursor``; together they drive infinite scroll with
 * a stable PK tiebreaker applied server-side.
 */
export interface CursorMeta {
  firstArg: string;
  afterArg: string;
  pageInfoField: string;
  hasNextPageField: string;
  endCursorField: string;
}

/**
 * Filter metadata for a table's list field.
 *
 * The schema's filter input is a ``@oneOf`` ``<Table>Filter`` whose ``field``
 * sub-input (also ``@oneOf``) carries one lookup per column. Multi-column AND
 * is expressed via the ``all`` combinator (a list of ``<Table>Filter``).
 * ``fieldKey``/``allKey`` and every operator name are discovered (never
 * hardcoded), so the code is agnostic to the schema's field casing.
 */
export interface FilterMeta {
  argName: string;
  filterSdl: string;
  fieldKey: string;
  allKey: string;
  columns: Map<string, FilterColumnMeta>;
}

/**
 * A relationship field on a row type.
 *
 * The GraphQL schema exposes relationships as nested object fields: to-one as
 * ``Target`` and to-many as ``[Target!]``. ``targetType`` is the related row
 * type name (matched back to a {@link TableMeta} via its ``rowType``).
 */
export interface RelationMeta {
  /** Relation field name on the row type, e.g. "author" / "books". */
  name: string;
  label: string;
  /** ``true`` for to-many (list) relations, ``false`` for to-one. */
  toMany: boolean;
  /** Related row type name, e.g. "Author". */
  targetType: string;
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
  cursor: CursorMeta | null;
  filter: FilterMeta | null;
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
  /** Relationship fields on the row type (object / list-of-object). */
  relations: RelationMeta[];
  pkColumns: string[];
  /** The singular primary-key lookup query field (for lazy detail fetches). */
  pkLookup: { field: string; pkArgs: ArgMeta[] } | null;
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
  while ((current.kind === 'NON_NULL' || current.kind === 'LIST') && current.ofType) {
    current = current.ofType;
  }
  return current;
}

/** ``true`` if ``ref`` has a ``LIST`` anywhere in its wrapper chain. */
function hasListWrapper(ref: TypeRef): boolean {
  let current: TypeRef | null = ref;
  while (current) {
    if (current.kind === 'LIST') return true;
    current = current.ofType;
  }
  return false;
}

function typeRefToSDL(ref: TypeRef): string {
  if (ref.kind === 'NON_NULL' && ref.ofType) return `${typeRefToSDL(ref.ofType)}!`;
  if (ref.kind === 'LIST' && ref.ofType) return `[${typeRefToSDL(ref.ofType)}]`;
  return ref.name ?? 'Unknown';
}

function humanize(name: string): string {
  return name
    .replace(/_/g, ' ')
    .replace(/([a-z0-9])([A-Z])/g, '$1 $2')
    .replace(/\s+/g, ' ')
    .trim()
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

function editorForColumn(named: TypeRef): EditorKind {
  if (named.kind === 'ENUM') return 'enum';
  switch (named.name) {
    case 'Int':
      return 'integer';
    case 'Float':
    case 'Decimal':
    case 'BigDecimal':
      return 'number';
    case 'Boolean':
      return 'boolean';
    case 'DateTime':
    case 'NaiveDateTime':
    case 'Timestamp':
      return 'datetime';
    case 'Date':
      return 'date';
    case 'JSON':
    case 'JSONB':
      return 'json';
    default:
      return 'text';
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
  const mutationType = schema.mutationType ? typeMap.get(schema.mutationType.name) : undefined;
  const mutationFields = new Map<string, FieldDef>();
  for (const f of mutationType?.fields ?? []) {
    mutationFields.set(f.name, f);
  }

  const sort = discoverSortDirection(typeMap);
  const tables: TableMeta[] = [];

  for (const field of queryType?.fields ?? []) {
    const connNamed = namedRef(field.type);
    if (connNamed.kind !== 'OBJECT' || !connNamed.name?.endsWith('Connection')) {
      continue;
    }
    const connType = typeMap.get(connNamed.name);
    if (!connType?.fields) continue;

    // The connection exposes a flat ``nodes`` list and ``totalCount`` (plus
    // relay ``edges``/``pageInfo``). Prefer the conventional field names, but
    // fall back to shape: ``nodes`` is the list-of-OBJECT whose element type
    // is not an ``*Edge``; ``totalCount`` is the scalar field.
    const nodesFieldDef =
      connType.fields.find((f) => f.name === 'nodes') ??
      connType.fields.find((f) => {
        const n = namedRef(f.type);
        return n.kind === 'OBJECT' && !n.name?.endsWith('Edge');
      });
    const totalCountFieldDef =
      connType.fields.find((f) => f.name === 'totalCount') ??
      connType.fields.find((f) => namedRef(f.type).kind === 'SCALAR');
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

    const pkNames = new Set((update?.pkArgs ?? remove?.pkArgs ?? []).map((a) => a.name));
    const inputFieldNames = create ? inputFieldNameSet(typeMap, create.inputArg) : new Set<string>();
    const patchFieldNames = update ? inputFieldNameSet(typeMap, update.patchArg) : new Set<string>();

    // Columns: scalar/enum row fields. Relations: object / list-of-object
    // fields (to-one / to-many) — collected separately for the detail panel.
    const columns: ColumnMeta[] = [];
    const relations: RelationMeta[] = [];
    for (const f of rowType.fields) {
      const named = namedRef(f.type);
      if (named.kind === 'SCALAR' || named.kind === 'ENUM') {
        columns.push({
          name: f.name,
          scalarType: named.name ?? 'Unknown',
          editor: editorForColumn(named),
          enumValues:
            named.kind === 'ENUM' && named.name
              ? (typeMap.get(named.name)?.enumValues ?? []).map((e) => e.name)
              : undefined,
          nullable: f.type.kind !== 'NON_NULL',
          isPk: pkNames.has(f.name),
          creatable: inputFieldNames.has(f.name),
          updatable: patchFieldNames.has(f.name),
        });
      } else if (named.kind === 'OBJECT' && named.name) {
        relations.push({
          name: f.name,
          label: humanize(f.name),
          toMany: hasListWrapper(f.type),
          targetType: named.name,
        });
      }
    }

    // Pagination args by name (the backend exposes literal limit/offset).
    const limitArg = field.args.find((a) => a.name.toLowerCase() === 'limit');
    const offsetArg = field.args.find((a) => a.name.toLowerCase() === 'offset');
    const order = buildOrderMeta(typeMap, field, columns);
    const cursor = buildCursorMeta(typeMap, field, connType);
    const filter = buildFilterMeta(typeMap, field, columns);
    const pkLookup = buildPkLookupMeta(queryType, rowTypeName, pkNames);

    tables.push({
      name: field.name,
      label: humanize(field.name),
      listField: field.name,
      rowType: rowTypeName,
      nodesField: nodesFieldDef.name,
      totalCountField: totalCountFieldDef.name,
      limitArg: limitArg?.name ?? null,
      offsetArg: offsetArg?.name ?? null,
      cursor,
      filter,
      order,
      columns,
      relations,
      pkColumns: [...pkNames],
      pkLookup,
      create,
      update,
      remove,
    });
  }

  tables.sort((a, b) => a.label.localeCompare(b.label));
  return { tables, sortAsc: sort.asc, sortDesc: sort.desc };
}

function buildUpdateMeta(def: FieldDef | undefined): TableMeta['update'] {
  if (!def) return null;
  // Detect the patch arg by name (resilient to the input type's name, which
  // strawberry_orm derives independently); the remaining args are the PK(s).
  const patch =
    def.args.find((a) => a.name === 'patch') ??
    def.args.find((a) => namedRef(a.type).kind === 'INPUT_OBJECT');
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

function buildCreateMeta(def: FieldDef | undefined): TableMeta['create'] {
  if (!def) return null;
  // The single-row create takes one INPUT_OBJECT argument (named ``input``).
  const input =
    def.args.find((a) => a.name === 'input') ??
    def.args.find((a) => namedRef(a.type).kind === 'INPUT_OBJECT');
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
): TableMeta['order'] {
  // The order arg is a LIST whose element is an INPUT_OBJECT.
  const arg = field.args.find((a) => {
    const inner = a.type.kind === 'NON_NULL' ? a.type.ofType : a.type;
    return inner?.kind === 'LIST' && namedRef(a.type).kind === 'INPUT_OBJECT';
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
    if (subNamed.kind !== 'INPUT_OBJECT' || !subNamed.name) continue;
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
    elementSdl: elementName ?? 'Unknown',
    fieldKey,
    sortableColumns,
  };
}

/**
 * Discover the singular primary-key lookup query field for a row type.
 *
 * The backend attaches one field per table that returns a single record by its
 * primary key (e.g. ``author(id: Int!)``). We find the Query field whose return
 * type is the row type (not a ``*Connection``) and whose argument names match
 * the primary-key columns, capturing its name and argument SDLs so the detail
 * panel can lazily fetch a row's relations.
 */
function buildPkLookupMeta(
  queryType: TypeDef | undefined,
  rowTypeName: string,
  pkNames: Set<string>,
): TableMeta['pkLookup'] {
  if (!queryType?.fields || pkNames.size === 0) return null;
  for (const field of queryType.fields) {
    const named = namedRef(field.type);
    if (named.kind !== 'OBJECT' || named.name !== rowTypeName) continue;
    if (field.args.length !== pkNames.size) continue;
    if (!field.args.every((a) => pkNames.has(a.name))) continue;
    return {
      field: field.name,
      pkArgs: field.args.map((a) => ({ name: a.name, sdl: typeRefToSDL(a.type) })),
    };
  }
  return null;
}

/**
 * Discover cursor-pagination metadata from the list field and its connection.
 *
 * Reads the ``first``/``after`` arguments off the list field and the
 * ``pageInfo`` object (and its ``hasNextPage``/``endCursor`` sub-fields) off
 * the connection type. Conventional names are preferred with a shape fallback
 * (``pageInfo`` is the OBJECT field whose own fields include a Boolean and a
 * nullable String). Returns ``null`` if the cursor surface is incomplete.
 */
function buildCursorMeta(
  typeMap: Map<string, TypeDef>,
  field: FieldDef,
  connType: TypeDef,
): CursorMeta | null {
  const firstArg = field.args.find((a) => a.name.toLowerCase() === 'first');
  const afterArg = field.args.find((a) => a.name.toLowerCase() === 'after');
  if (!firstArg || !afterArg) return null;

  const pageInfoFieldDef =
    connType.fields?.find((f) => f.name === 'pageInfo') ??
    connType.fields?.find((f) => {
      const n = namedRef(f.type);
      const def = n.name ? typeMap.get(n.name) : undefined;
      return n.kind === 'OBJECT' && (def?.fields?.length ?? 0) >= 2;
    });
  if (!pageInfoFieldDef) return null;
  const pageInfoTypeName = namedRef(pageInfoFieldDef.type).name;
  const pageInfoType = pageInfoTypeName ? typeMap.get(pageInfoTypeName) : undefined;
  if (!pageInfoType?.fields) return null;

  const hasNextPageDef =
    pageInfoType.fields.find((f) => f.name === 'hasNextPage') ??
    pageInfoType.fields.find((f) => namedRef(f.type).name === 'Boolean');
  const endCursorDef =
    pageInfoType.fields.find((f) => f.name === 'endCursor') ??
    pageInfoType.fields.find(
      (f) => f.type.kind !== 'NON_NULL' && namedRef(f.type).name === 'String',
    );
  if (!hasNextPageDef || !endCursorDef) return null;

  return {
    firstArg: firstArg.name,
    afterArg: afterArg.name,
    pageInfoField: pageInfoFieldDef.name,
    hasNextPageField: hasNextPageDef.name,
    endCursorField: endCursorDef.name,
  };
}

/** Resolve an operator input value to its expected value kind + scalar type. */
function operatorMeta(op: InputValue): OperatorMeta {
  const inner = op.type.kind === 'NON_NULL' ? (op.type.ofType ?? op.type) : op.type;
  // Lists (in_list / not_in_list) accept multiple comma-separated values.
  if (inner.kind === 'LIST') {
    return { name: op.name, valueKind: 'list', scalarType: namedRef(op.type).name ?? 'String' };
  }
  const named = namedRef(op.type);
  // Range inputs (e.g. IntRangeInput) are objects with start/end.
  if (named.kind === 'INPUT_OBJECT') {
    return { name: op.name, valueKind: 'range', scalarType: named.name ?? 'Unknown' };
  }
  if (named.name === 'Boolean') {
    return { name: op.name, valueKind: 'boolean', scalarType: 'Boolean' };
  }
  return { name: op.name, valueKind: 'scalar', scalarType: named.name ?? 'String' };
}

/**
 * Discover the list field's ``filter`` argument shape.
 *
 * The filter input is a ``@oneOf`` ``<Table>Filter`` with a ``field`` sub-input
 * (per-column lookups) and an ``all`` combinator (list of ``<Table>Filter``).
 * For each column we read its lookup type's operators from introspection.
 * Returns ``null`` when the table exposes no usable column filters.
 */
function buildFilterMeta(
  typeMap: Map<string, TypeDef>,
  field: FieldDef,
  columns: ColumnMeta[],
): FilterMeta | null {
  const arg = field.args.find((a) => namedRef(a.type).kind === 'INPUT_OBJECT');
  if (!arg) return null;
  const filterTypeName = namedRef(arg.type).name;
  const filterType = filterTypeName ? typeMap.get(filterTypeName) : undefined;
  if (!filterType?.inputFields?.length) return null;

  const columnNames = new Set(columns.map((c) => c.name));

  // The ``field`` sub-input: an INPUT_OBJECT whose own fields are our columns.
  let fieldKey: string | null = null;
  let fieldDef: TypeDef | undefined;
  for (const sub of filterType.inputFields) {
    const named = namedRef(sub.type);
    if (named.kind !== 'INPUT_OBJECT' || !named.name) continue;
    const def = typeMap.get(named.name);
    const matching = (def?.inputFields ?? []).filter((f) => columnNames.has(f.name));
    if (matching.length > 0) {
      fieldKey = sub.name;
      fieldDef = def;
      break;
    }
  }
  if (!fieldKey || !fieldDef?.inputFields) return null;

  // The ``all`` combinator: a LIST whose element is the filter type itself.
  const allArg = filterType.inputFields.find((sub) => {
    const inner = sub.type.kind === 'NON_NULL' ? sub.type.ofType : sub.type;
    return inner?.kind === 'LIST' && namedRef(sub.type).name === filterTypeName;
  });
  if (!allArg) return null;

  const filterColumns = new Map<string, FilterColumnMeta>();
  for (const colField of fieldDef.inputFields) {
    if (!columnNames.has(colField.name)) continue;
    const lookupName = namedRef(colField.type).name;
    const lookupDef = lookupName ? typeMap.get(lookupName) : undefined;
    if (!lookupDef?.inputFields?.length) continue;
    filterColumns.set(colField.name, {
      name: colField.name,
      operators: lookupDef.inputFields.map(operatorMeta),
    });
  }
  if (filterColumns.size === 0) return null;

  return {
    argName: arg.name,
    filterSdl: filterTypeName ?? 'Unknown',
    fieldKey,
    allKey: allArg.name,
    columns: filterColumns,
  };
}

function buildDeleteMeta(def: FieldDef | undefined): TableMeta['remove'] {
  if (!def) return null;
  return {
    field: def.name,
    pkArgs: def.args.map((a) => ({ name: a.name, sdl: typeRefToSDL(a.type) })),
  };
}

function inputFieldNameSet(typeMap: Map<string, TypeDef>, arg: ArgMeta): Set<string> {
  // Recover the named INPUT_OBJECT from the SDL (strip wrapping !/[]).
  const named = arg.sdl.replace(/[![\]]/g, '');
  const def = typeMap.get(named);
  return new Set((def?.inputFields ?? []).map((f) => f.name));
}

function discoverSortDirection(typeMap: Map<string, TypeDef>): { asc: string; desc: string } {
  // Find the sort-direction enum: an ENUM with ASC- and DESC-like values.
  for (const def of typeMap.values()) {
    if (def.kind !== 'ENUM' || !def.enumValues?.length) continue;
    const asc = def.enumValues.find((v) => v.name.toLowerCase().includes('asc'))?.name;
    const desc = def.enumValues.find((v) => v.name.toLowerCase().includes('desc'))?.name;
    if (asc && desc) return { asc, desc };
  }
  return { asc: 'ASC', desc: 'DESC' };
}

// ---- Query / mutation string builders ----

function selectionColumns(meta: TableMeta): string {
  return meta.columns.map((c) => c.name).join(' ');
}

// ---- Relations ----

/** Resolve a relation's target table (matched by row type), if it's listed. */
export function relationTargetTable(
  schema: DataSchema,
  relation: RelationMeta,
): TableMeta | undefined {
  return schema.tables.find((t) => t.rowType === relation.targetType);
}

const DISPLAY_COLUMN_HINTS = ['name', 'title', 'label', 'slug', 'email', 'code'];

/**
 * Pick a representative "display" column for a table.
 *
 * Prefers a conventional label column (name/title/label/…), else the first
 * non-PK text column, else the first primary-key column. Used for link text and
 * compact related-record rendering.
 */
export function displayColumn(meta: TableMeta): ColumnMeta | undefined {
  const byHint = meta.columns.find((c) => DISPLAY_COLUMN_HINTS.includes(c.name.toLowerCase()));
  if (byHint) return byHint;
  const text = meta.columns.find((c) => c.editor === 'text' && !c.isPk);
  if (text) return text;
  return meta.columns.find((c) => c.isPk) ?? meta.columns[0];
}

/** Stable string key for a row, derived from its primary-key value(s). */
export function rowKey(meta: TableMeta, row: Record<string, unknown>): string {
  return meta.pkColumns.map((c) => String(row[c])).join('\u241f');
}

/**
 * Build the singular primary-key lookup query selecting one level of relations.
 *
 * Each relation is selected with the target table's scalar columns (no
 * nested-of-nested relations), so the detail panel can render related records
 * without risking deep/cyclic queries. Returns ``null`` when the table has no
 * PK-lookup field or no relations.
 */
export function buildDetailQuery(meta: TableMeta, schema: DataSchema): string | null {
  if (!meta.pkLookup || meta.relations.length === 0) return null;
  const { field, pkArgs } = meta.pkLookup;
  const varDefs = pkArgs.map((a) => `$${a.name}: ${a.sdl}`);
  const callArgs = pkArgs.map((a) => `${a.name}: $${a.name}`);
  const relationSelections = meta.relations.map((rel) => {
    const target = relationTargetTable(schema, rel);
    const cols = target ? selectionColumns(target) : '__typename';
    return `${rel.name} { __typename ${cols} }`;
  });
  return `query DataDetail(${varDefs.join(', ')}) {
  ${field}(${callArgs.join(', ')}) {
    __typename
    ${relationSelections.join('\n    ')}
  }
}`;
}

/** A single order entry, e.g. ``{ field: { id: ASC } }``. */
export type OrderValue = Record<string, Record<string, string>>;

/**
 * Build the list query.
 *
 * Prefers cursor (keyset) pagination (``first``/``after`` + ``pageInfo``) when
 * the table exposes it — driving infinite scroll — and includes the ``filter``
 * argument when available. Falls back to ``limit``/``offset`` otherwise.
 */
export function buildListQuery(meta: TableMeta): string {
  const varDefs: string[] = [];
  const args: string[] = [];
  if (meta.cursor) {
    varDefs.push('$first: Int', '$after: String');
    args.push(`${meta.cursor.firstArg}: $first`, `${meta.cursor.afterArg}: $after`);
  } else {
    if (meta.limitArg) {
      varDefs.push('$limit: Int');
      args.push(`${meta.limitArg}: $limit`);
    }
    if (meta.offsetArg) {
      varDefs.push('$offset: Int');
      args.push(`${meta.offsetArg}: $offset`);
    }
  }
  if (meta.order) {
    varDefs.push(`$order: [${meta.order.elementSdl}!]`);
    args.push(`${meta.order.argName}: $order`);
  }
  if (meta.filter) {
    varDefs.push(`$filter: ${meta.filter.filterSdl}`);
    args.push(`${meta.filter.argName}: $filter`);
  }
  const head = varDefs.length ? `(${varDefs.join(', ')})` : '';
  const argStr = args.length ? `(${args.join(', ')})` : '';
  const pageInfo = meta.cursor
    ? `${meta.cursor.pageInfoField} { ${meta.cursor.hasNextPageField} ${meta.cursor.endCursorField} }`
    : '';
  return `query DataList${head} {
  ${meta.listField}${argStr} {
    ${meta.totalCountField}
    ${pageInfo}
    ${meta.nodesField} { __typename ${selectionColumns(meta)} }
  }
}`;
}

/** An active per-column filter chosen in the UI. */
export interface ActiveFilter {
  op: string;
  value: unknown;
}

/**
 * Build the ``filter`` variable value from the active per-column filters.
 *
 * Honours the schema's ``@oneOf`` constraints: a single column filter is
 * ``{ field: { col: { op: v } } }``; multiple columns are AND-combined via
 * ``{ all: [ … ] }`` (each entry sets exactly one column). Returns ``null``
 * when nothing is active or the table has no filter surface.
 */
export function buildFilterValue(
  meta: TableMeta,
  filters: Record<string, ActiveFilter>,
): Record<string, unknown> | null {
  if (!meta.filter) return null;
  const { fieldKey, allKey, columns } = meta.filter;
  const entries: Record<string, unknown>[] = [];
  for (const [colName, active] of Object.entries(filters)) {
    const colMeta = columns.get(colName);
    const opMeta = colMeta?.operators.find((o) => o.name === active.op);
    if (!opMeta) continue;
    const value = coerceFilterValue(opMeta, active.value);
    if (value === undefined) continue;
    entries.push({ [fieldKey]: { [colName]: { [opMeta.name]: value } } });
  }
  if (entries.length === 0) return null;
  if (entries.length === 1) return entries[0];
  return { [allKey]: entries };
}

/** Coerce a raw editor value to the JSON shape a filter operator expects. */
function coerceFilterValue(op: OperatorMeta, raw: unknown): unknown {
  // For ``range`` the scalarType is the range input name (e.g. "IntRangeInput");
  // infer the element numeric-ness from it. For scalar/list it's the scalar name.
  const isInt = op.scalarType.startsWith('Int');
  const isFloatish = op.scalarType.startsWith('Float') || op.scalarType.startsWith('Decimal');
  const isNumeric = isInt || isFloatish;
  const coerceScalar = (v: unknown): unknown => {
    if (op.scalarType === 'Boolean') return v === true || v === 'true';
    if (!isNumeric) return v;
    const n = isInt ? Number.parseInt(String(v), 10) : Number.parseFloat(String(v));
    return Number.isNaN(n) ? undefined : n;
  };
  switch (op.valueKind) {
    case 'boolean':
      return raw === true || raw === 'true';
    case 'list': {
      const parts = String(raw ?? '')
        .split(',')
        .map((s) => s.trim())
        .filter((s) => s !== '')
        .map(coerceScalar)
        .filter((v) => v !== undefined);
      return parts.length ? parts : undefined;
    }
    case 'range': {
      const r = raw as { start?: unknown; end?: unknown } | null;
      if (!r) return undefined;
      const start = coerceScalar(r.start);
      const end = coerceScalar(r.end);
      if (start === undefined || end === undefined) return undefined;
      return { start, end };
    }
    default: {
      if (raw === '' || raw === null || raw === undefined) return undefined;
      return coerceScalar(raw);
    }
  }
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
  return `mutation DataUpdate(${varDefs.join(', ')}) {
  ${field}(${callArgs.join(', ')}) { __typename ${selectionColumns(meta)} }
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
  const pkSelection = meta.pkColumns.join(' ') || '__typename';
  return `mutation DataDelete(${varDefs.join(', ')}) {
  ${field}(${callArgs.join(', ')}) { ${pkSelection} }
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
  if (raw === '' || raw === null || raw === undefined) {
    return column.nullable ? null : raw === '' ? null : raw;
  }
  switch (column.editor) {
    case 'integer': {
      const n = Number.parseInt(String(raw), 10);
      return Number.isNaN(n) ? null : n;
    }
    case 'number': {
      const n = Number.parseFloat(String(raw));
      return Number.isNaN(n) ? null : n;
    }
    case 'boolean':
      return Boolean(raw);
    case 'json':
      return typeof raw === 'string' ? JSON.parse(raw) : raw;
    default:
      return raw;
  }
}

/** Render a stored cell value for display in a read-only or text context. */
export function formatValue(value: unknown): string {
  if (value === null || value === undefined) return '';
  if (typeof value === 'object') return JSON.stringify(value);
  return String(value);
}

/** ``ArgMeta`` re-exported for consumers building PK variable maps. */
export type { ArgMeta };
