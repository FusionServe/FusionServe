# Custom Queries from PostgreSQL Functions

## Overview

In addition to the CRUD endpoints synthesised from each table, FusionServe can expose **PostgreSQL functions** as first-class queries on both the GraphQL and REST surfaces. Define a `STABLE` (or `IMMUTABLE`) function in your application schema, give it a comment, and it appears at startup as:

- a root field on the GraphQL `Query` type, and
- a `GET` endpoint under `/api/v1/<function_name>`.

This is the FusionServe equivalent of [PostgREST RPC](https://postgrest.org/en/stable/api.html#stored-procedures) / [Postgraphile custom queries](https://www.graphile.org/postgraphile/custom-queries/) — the database remains the source of truth, and complex read-side logic stays close to the data.

`VOLATILE` functions are deliberately not exposed in v1: they may have side effects and conceptually belong to the `Mutation` root, which is out of scope for this feature.

---

## Requirements

| Requirement | Detail |
|---|---|
| Schema | Function lives in `settings.pg_app_schema` (default `app_public`). |
| Volatility | `STABLE` or `IMMUTABLE` — `VOLATILE` is filtered out. |
| Kind | `prokind = 'f'` — ordinary function. Aggregates, window functions, and procedures are skipped. |
| Parameters | All IN, all named (no positional-only). `OUT` / `INOUT` / `VARIADIC` / `TABLE(...)` modes are not supported in v1. |
| Argument types | One of the supported scalars (see [Type Support](#type-support)). |
| Return type | Scalar of a supported type, a single row of an existing table, or `SETOF` an existing table. |
| Overloading | A function name must have **one** signature in the schema. Overloaded names are skipped wholesale with a logged warning. |
| Permissions | The PostgreSQL role used at request time needs `EXECUTE` on the function (and the privileges its body requires). Enforced at runtime, not at startup. |

A function that fails any check is skipped at startup with a single `WARNING` log line — the rest of the schema continues to load.

---

## Type Support

Both arguments and return scalars draw from the same map (`fusionserve.persistence._PG_TO_PY`):

| PostgreSQL type | GraphQL / Python | Notes |
|---|---|---|
| `int2`, `int4`, `int8` | `Int` | |
| `float4`, `float8` | `Float` | |
| `numeric` | `Decimal` | Serialised as a string on both surfaces. |
| `text`, `varchar`, `bpchar`, `name`, `citext` | `String` | |
| `bool` | `Boolean` | |
| `uuid` | `UUID` | |
| `date` | `Date` | |
| `time`, `timetz` | `Time` | |
| `timestamp`, `timestamptz` | `DateTime` | |
| `json`, `jsonb` | `JSON` (Strawberry built-in) | Lossless arbitrary JSON; on REST it round-trips as the parsed JSON value. |

Arrays, ranges, composite types other than the function's own table return, domains, refcursor, and inline `TABLE(...)` returns are not supported in v1.

---

## Return Shapes

| PG declaration | GraphQL | REST |
|---|---|---|
| Scalar (e.g. `RETURNS int`) | `Int` | `200 {"value": <scalar>}` |
| `RETURNS <table>` | `<TableType> \| null` | `200 <object>` or `404` if the function returned no row. |
| `RETURNS SETOF <table>` | `[<TableType>]` | `200 [<object>, …]` |

The table referenced by `RETURNS [SETOF] <table>` must already be reflected by automap (i.e. it lives in `pg_app_schema`). If you need the result shape of an ad-hoc projection, define a real table or view; inline `TABLE(...)` is not enough.

---

## Naming

`my_function_name(...)` becomes:

- `myFunctionName` on GraphQL (camelCase, via `strawberry.utils.str_converters.to_camel_case`).
- `/api/v1/my_function_name` on REST (snake_case, matching PostgreSQL).

If the GraphQL field name collides with an existing query field (table list / by-PK), or the REST path collides with an existing table route, the function is **skipped with a logged warning** — the table-driven route always wins. A `# TODO` next to the skip points marks this as the spot for a future opt-in collision-resolution strategy (e.g. namespace prefix, smart-comment-driven override).

---

## Authorisation

Both surfaces call [`set_role`](../../src/fusionserve/persistence.py) on the request session before invoking the function, so the function body executes under the authenticated user's PostgreSQL role (or `settings.anonymous_role` for unauthenticated requests).

- `SECURITY INVOKER` (the default) — the function inherits Row-Level Security policies and column privileges of the calling role. Recommended for most cases.
- `SECURITY DEFINER` — the function runs as its **owner**. Useful for trusted privilege elevation (e.g. an audit log helper) but dangerous if the body trusts client input. Use with care.
- A user without `EXECUTE` privilege gets a PostgreSQL error, surfaced as a GraphQL error or `500` from REST.

See [Security](security.md) for the broader auth/RLS picture.

---

## Smart Comments

The function `COMMENT` is parsed by the same [smart-comment](smart_comments.md) machinery used for tables and columns:

- The plain-text body becomes the GraphQL field's `description` (visible in introspection / GraphiQL hover) and the REST endpoint's `description` (visible in the OpenAPI spec).
- Optional YAML frontmatter is parsed and stored on `FunctionInfo.metadata`. It is **not** consulted by the runtime in v1 — reserved for forthcoming authorisation hooks.

```sql
COMMENT ON FUNCTION app_public.users_search(text) IS $$
---
owner: identity-team
sla:
  cache_ttl: 60s
---
Substring-match search over user display names, ordered by relevance.
$$;
```

Per-argument descriptions are not supported because PostgreSQL has no per-argument `COMMENT` mechanism. Document arguments inside the function body comment.

---

## Example

### SQL

```sql
CREATE FUNCTION app_public.users_search(query text, max_results int DEFAULT 50)
RETURNS SETOF app_public.users
LANGUAGE sql STABLE
AS $$
  SELECT *
  FROM app_public.users
  WHERE display_name ILIKE '%' || query || '%'
  ORDER BY display_name
  LIMIT max_results;
$$;

COMMENT ON FUNCTION app_public.users_search(text, int)
IS 'Substring-match search over users by display name.';
```

### GraphQL

```graphql
{
  usersSearch(query: "ada", maxResults: 10) {
    id
    displayName
    email
  }
}
```

The `maxResults` argument is **optional** because the PostgreSQL function declares a `DEFAULT`; omit it and the server-side default is honoured.

### REST

```
GET /api/v1/users_search?query=ada&max_results=10
```

```json
[
  { "id": "…", "display_name": "Ada Lovelace", "email": "…" },
  …
]
```

A scalar function instead returns the wrapped form:

```sql
CREATE FUNCTION app_public.greeting(name text)
RETURNS text LANGUAGE sql IMMUTABLE
AS $$ SELECT 'Hello, ' || name $$;
```

```
GET /api/v1/greeting?name=Ada
```

```json
{ "value": "Hello, Ada" }
```

---

## Limitations (v1)

- `VOLATILE` functions are not exposed (no `Mutation` wiring yet).
- Inline `TABLE(...)` and anonymous `RECORD` returns are not supported — wrap the projection in a real table or view.
- Composite IN parameters, arrays, ranges, refcursor, and domains are not supported.
- Overloaded function names are skipped wholesale; rename or drop the duplicates.
- Per-argument descriptions are unavailable (PostgreSQL limitation).
- Name collisions with table-driven routes are skipped (the table wins). A future release will introduce an opt-in resolution strategy.
- `Decimal` and `UUID` round-trip as strings on both surfaces — this matches Strawberry/Pydantic defaults but may surprise consumers expecting raw JSON numbers.

## Implementation pointers

| Concern | Location |
|---|---|
| Function discovery | [`fusionserve.persistence._introspect_functions`](../../src/fusionserve/persistence.py) |
| PostgreSQL → Python type map | `fusionserve.persistence._PG_TO_PY` |
| GraphQL wiring | [`fusionserve.graphql._build_function_resolver` and `build`](../../src/fusionserve/graphql.py) |
| REST wiring | [`fusionserve.rest.build_function_controllers`](../../src/fusionserve/rest.py) |
| Bundled introspection result | [`fusionserve.models.Introspection`](../../src/fusionserve/models.py) |
