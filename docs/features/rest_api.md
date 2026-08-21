# REST API

## Overview

FusionServe automatically generates a complete **CRUD REST API** for every table discovered during schema introspection.  Each table is mapped to a [Litestar `Controller`](../../src/fusionserve/rest.py) with five endpoints, following REST best practices for resource naming and HTTP semantics.

---

## Endpoint Structure

For a table named `{resource}` (e.g. `users`, `invoices`):

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/v1/{resource}` | List records — supports pagination, basic filtering, and OData filtering |
| `GET` | `/api/v1/{resource}/{pk}` | Fetch a single record by primary key |
| `POST` | `/api/v1/{resource}` | Create a new record |
| `PATCH` | `/api/v1/{resource}/{pk}` | Partially update an existing record |
| `DELETE` | `/api/v1/{resource}/{pk}` | Delete a record |

> **Plural table names are required.**  FusionServe uses the [`inflect`](https://pypi.org/project/inflect/) library to derive the singular noun used in response summaries and error messages from the plural table name.

---

## Composite Primary Keys

Path parameters are derived automatically from the table's primary key columns.  For composite primary keys every component is included in the path as a UUID segment:

```
GET /api/v1/order_items/{order_id}/{item_id}
```

The path template is built dynamically at startup:

```python
f"/{'/'.join([f'{{{pk}:uuid}}' for pk in pkeys])}"
```

---

## Request & Response Bodies

All request and response bodies are validated against the Pydantic models generated during introspection:

| Operation | Body model | Notes |
|---|---|---|
| `GET` list | — | Response is `list[Model]` |
| `GET` single | — | Response is `Model` |
| `POST` | `Model` | `None` fields are excluded from the INSERT |
| `PATCH` | `Model` | Only non-`None`, explicitly-set fields are written |
| `DELETE` | — | Returns `204 No Content` |

---

## Endpoint Behaviour Details

### `GET /api/v1/{resource}` — List

```
GET /api/v1/users?_limit=20&_offset=0&role=admin&_filter=(age gt 18)
```

1. Applies **limit/offset pagination** from the `_limit` / `_offset` query parameters.
2. Applies **basic equality filters** from any query parameter matching a column name (e.g. `?role=admin`).
3. Applies an optional **OData advanced filter** via the `_filter` parameter.

If the OData expression is syntactically invalid a `400 Bad Request` is returned with a descriptive error message.

### `GET /api/v1/{resource}/{pk}` — Get One

Returns the record matching the given primary key.  Raises `404 Not Found` if no matching record exists.

### `POST /api/v1/{resource}` — Create

Inserts a new row.  The request body is validated against the `Model`.  `None` fields are excluded from the `INSERT` statement so that database defaults are respected.  The inserted record (including any server-generated columns) is refreshed and returned.

### `PATCH /api/v1/{resource}/{pk}` — Update

Fetches the existing record, then applies only the fields that were explicitly provided and are non-`None`.  Returns `404 Not Found` if the record does not exist.

```python
for k, v in data.model_dump(exclude_unset=True, exclude_none=True).items():
    setattr(record, k, v)
```

### `DELETE /api/v1/{resource}/{pk}` — Delete

Deletes the record.  Returns `404 Not Found` if it does not exist.

---

## OpenAPI Tags

Each controller is automatically tagged with the table name and its human-readable description extracted from the table comment (plain-text portion only, after stripping YAML frontmatter):

```python
tags = [f"{table.name}: {comment.content if comment.content else ''}"]
```

This groups endpoints cleanly in Swagger UI and Scalar.

---

## Controller Registration

Controllers are built by [`build()`](../../src/fusionserve/rest.py) and registered on the Litestar application inside the `lifespan` context. Custom-query (PostgreSQL function) controllers are built by [`build_function_controllers()`](../../src/fusionserve/rest.py):

```python
for controller in rest.build(introspection):
    app.register(controller)
for controller in rest.build_function_controllers(introspection):
    app.register(controller)
```

Each controller is a distinct Litestar `Controller` subclass created at runtime via [`create_controller()`](../../src/fusionserve/rest.py). Response and query Pydantic models are derived directly from each ORM class's `__table__` at controller-creation time — there is no shared model registry.

---

## Error Handling

| Condition | HTTP Status |
|---|---|
| Record not found (GET/PATCH/DELETE) | `404 Not Found` |
| Invalid OData filter expression | `400 Bad Request` |
| Validation error in request body | `422 Unprocessable Entity` |

---

## Session Handling

Every handler receives an [`AsyncSession`](../../src/fusionserve/persistence.py) via Litestar dependency injection.  Before executing any query, [`set_role()`](../../src/fusionserve/persistence.py) is called to switch the PostgreSQL session to the request's role — the authenticated user's role (`user.role`), falling back to the configured `anonymous_role` only for unauthenticated requests — enforcing row-level security and permission constraints defined at the database level.
