# GraphQL API

## Overview

In addition to the REST API, FusionServe generates a **GraphQL schema** from the same introspected database metadata, exposing every table as a queryable type with built-in pagination and field-selection push-down.  The GraphQL endpoint is served at `/graphql` via [Strawberry](https://strawberry.rocks/) and [strawberry-sqlalchemy-mapper](https://pypi.org/project/strawberry-sqlalchemy-mapper/).

> **Note:** The GraphQL API is currently in active development.  The feature is wired up at startup alongside the REST API.

---

## Accessing the API

| Path | Description |
|---|---|
| `/graphql` | GraphQL Playground (interactive browser IDE) |

Queries via `GET` are disabled; only `POST` requests are accepted.

---

## Schema Generation

At startup, [`build()`](../../src/fusionserve/graphql.py) iterates the models registry and:

1. Maps each ORM class to a Strawberry GraphQL type using `StrawberrySQLAlchemyMapper`.
2. Attaches a resolver function to the root `Query` type for each table.
3. Calls `mapper.finalize()` to resolve any related types automatically.
4. Builds the final `strawberry.Schema` with all mapped types registered.

```python
mapper = StrawberrySQLAlchemyMapper()
# ... map each table ...
mapper.finalize()
schema = strawberry.Schema(strawberry.type(Query), types=additional_types, ...)
```

---

## Pagination Window

Every table query returns a [`PaginationWindow`](../../src/fusionserve/graphql.py) wrapper type rather than a raw list, providing both the result nodes and the total dataset size in a single response:

```graphql
type PaginationWindow {
  nodes: [<TableType>!]!        # records in this page
  totalCount: Int!               # total matching records
}
```

**Example query:**

```graphql
query {
  users(limit: 10, offset: 0) {
    nodes {
      id
      name
      email
    }
    totalCount
  }
}
```

The `totalCount` is computed using a PostgreSQL window function (`COUNT() OVER()`) in the same query, so no second round-trip to the database is required.

---

## Resolver Arguments

Every auto-generated resolver accepts the following arguments:

| Argument | Type | Default | Description |
|---|---|---|---|
| `limit` | `Int` | `max_page_size` | Maximum records to return |
| `offset` | `Int` | `0` | Records to skip |
| `order_by` | `String` | `null` | Column name to sort by |

---

## Field Selection Push-down

The resolver inspects the GraphQL query's [`selected_fields`](../../src/fusionserve/graphql.py) and translates them into a SQLAlchemy `load_only()` directive.  Only the columns actually requested in the query are fetched from the database, reducing I/O for wide tables:

```python
statement = (
    select(orm_class, func.count().over().label("total_count"))
    .options(load_only(*get_selected_fields(info, gql_type)))
    .limit(limit)
    .offset(offset)
)
```

---

## Query Depth Limiting

To protect against deeply nested or circular queries, the schema is created with a [`QueryDepthLimiter`](https://strawberry.rocks/docs/extensions/query-depth-limiter) extension that rejects queries exceeding a nesting depth of **10**:

```python
extensions=[QueryDepthLimiter(max_depth=10)]
```

---

## Keep-Alive

The GraphQL router is configured with `keep_alive=True`, enabling WebSocket keep-alive pings for long-lived subscription or watch connections.

---

## Context

Each request receives a context dictionary containing:

| Key | Value |
|---|---|
| `session` | An active `AsyncSession` scoped to the request |
| `sqlalchemy_loader` | A `StrawberrySQLAlchemyLoader` for efficient relationship loading |

The session is provided via the same [`get_async_session`](../../src/fusionserve/persistence.py) dependency used by the REST API, ensuring consistent connection pooling and role enforcement across both APIs.

---

## Role Enforcement

Before executing the database query, every resolver calls [`set_role()`](../../src/fusionserve/persistence.py) to switch the PostgreSQL session to the configured `anonymous_role`, consistent with the REST API's security model.
