# GraphQL API

## Overview

In addition to the REST API, FusionServe generates a **GraphQL schema** from the same introspected database metadata, exposing every table as a queryable type with relay-style pagination, native filtering/ordering, relationship traversal, and full CRUD mutations. The endpoint is served at `/graphql` via [Strawberry](https://strawberry.rocks/) and [strawberry-orm](https://pypi.org/project/strawberry-orm/) (SQLAlchemy backend).

> **Note:** The GraphQL builder is wired up at startup alongside the REST API. It is built dynamically from live PostgreSQL introspection — there is no codegen step.

---

## Accessing the API

| Path | Description |
|---|---|
| `/graphql` | GraphiQL (interactive browser IDE) and the POST endpoint |

Queries via `GET` are disabled; only `POST` requests are accepted.

---

## Schema Generation

At startup, [`build()`](../../src/fusionserve/graphql.py) iterates the introspected automap classes (in a stable, name-sorted order) in two passes:

1. **Loop A** — registers a native `orm.filter` and `orm.order` input type per table and pre-creates a bare GraphQL type class for every table (so cyclic relationships resolve).
2. **Loop B** — decorates each type with `orm.type(...)`, applies smart-comment descriptions, and attaches its root fields (connection/pk query + CRUD mutations).

A single per-build `StrawberryORM.for_sqlalchemy(...)` instance drives the process, and the schema is created with `orm.schema(...)` (the N+1 query optimizer is enabled by default). Custom-query fields are then added for STABLE/IMMUTABLE PostgreSQL functions.

---

## Queries

### Connections (relay)

Each table with a single-column primary key is exposed as a relay node, and its top-level query is a connection with `edges`/`node`/`pageInfo` and `totalCount`:

```graphql
query {
  users(first: 10, after: "…", filter: { field: { name: { exact: "Ada" } } }, order: [{ field: { name: ASC } }]) {
    totalCount
    edges { cursor node { id name email } }
    pageInfo { hasNextPage endCursor }
  }
}
```

Composite-PK tables fall back to a plain list field (no relay node id).

### Primary-key lookup

A `<singular>(…pk args)` field returns a single record by primary key (raw column values, not a relay global id).

### Filtering & ordering

Filters are native `@oneOf` trees (`field`, `object`, `all`, `any`, `not`, `oneOf`); ordering is a list of `@oneOf` entries. See the [strawberry-orm docs](https://pypi.org/project/strawberry-orm/) for the full lookup shapes.

> **Known limitation:** for a bidirectional (automap) relationship only one direction exposes a nested `object` filter; the wired direction is deterministic (name-sorted registration). See the design spec's friction log.

---

## Mutations

For every non-view table, six CRUD mutations are generated (RETURNING-based, single round-trip):

| Mutation | Input |
|---|---|
| `create<Singular>` | `orm.input` |
| `create<Plural>` | `[orm.input]` |
| `update<Singular>` | `orm.partial` + pk args |
| `update<Plural>` | `orm.partial` + `where` filter |
| `delete<Singular>` | pk args |
| `delete<Plural>` | `where` filter |

`update<Plural>`/`delete<Plural>` reject an empty/`None`-resolving `where` to block accidental table-wide writes.

> Mutation payloads should select scalar columns; selecting a nested relation on a mutation result is not currently supported (async lazy-load).

---

## Custom queries from PostgreSQL functions

Each STABLE/IMMUTABLE function in the app schema becomes a Query field (camelCased): `SCALAR` returns map to the mapped Python type, `ROW`/`SET` returns to the generated node type(s). Functions whose name collides with an existing field, or whose return table has no mapped type, are skipped with a logged warning.

---

## Query Depth Limiting

The schema is built with a [`QueryDepthLimiter`](https://strawberry.rocks/docs/extensions/query-depth-limiter) rejecting queries deeper than **10**.

---

## Context & Row-Level Security

Each request opens **one** `AsyncSession` ([`custom_context_getter`](../../src/fusionserve/graphql.py)) and stores it on the Strawberry context. The strawberry-orm backend's `session_getter` reuses that exact session for every query, optimizer eager-load, and nested relation load, so row-level security is consistent everywhere.

The request's PostgreSQL role (via [`set_role`](../../src/fusionserve/persistence.py)) is **re-applied on every transaction the session opens** through an `after_begin` hook — so a mutation's post-commit work still runs under the request's role, while the transaction-local setting keeps pooled connections role-free. Unauthenticated requests use `settings.anonymous_role`.

| Context attribute | Value |
|---|---|
| `session` | The request's role-scoped `AsyncSession` |
| `request` | The Litestar request (carries the authenticated user) |
