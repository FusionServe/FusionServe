# Pagination

## Overview

Every `GET` list endpoint in FusionServe supports **limit/offset pagination** via two reserved query parameters.  A configurable maximum page size prevents accidental or malicious retrieval of unbounded result sets.

---

## Query Parameters

| Parameter | Type | Default | Constraints | Description |
|---|---|---|---|---|
| `_limit` | `integer` | `20` | `>= 1` | Maximum number of records to return |
| `_offset` | `integer` | `0` | `>= 0` | Number of records to skip before returning results |

The leading underscore prefix is intentional — it prevents collisions with column names used for [basic equality filtering](filtering.md).

### Example

```
GET /api/users?_limit=10&_offset=20
```

Returns records 21–30 (zero-indexed).

---

## Maximum Page Size

The server-side maximum is controlled by the `max_page_length` configuration setting (default `1000`).  Requests that specify a `_limit` value above this ceiling are rejected with a validation error.

| Setting | Default | Description |
|---|---|---|
| `max_page_length` | `1000` | Absolute upper bound on `_limit` |

---

## How It Works

Pagination is implemented as a **Litestar dependency** using `advanced-alchemy`'s `LimitOffset` filter.  The dependency is created by [`create_filter_dependencies()`](../../src/fusionserve/di.py) and injected into each list handler:

```python
def provide_limit_offset_pagination(
    offset: int = Parameter(ge=0,  query="_offset", default=0,   required=False),
    limit:  int = Parameter(ge=1,  query="_limit",  default=20,  required=False),
) -> LimitOffset:
    return LimitOffset(limit, offset)
```

The resulting `LimitOffset` filter is appended to the SQLAlchemy `SELECT` statement before execution:

```python
statement = filters[0].append_to_statement(select(orm_class), orm_class)
```

---

## Combining with Filtering

Pagination and filtering are applied together.  The `_limit` / `_offset` parameters restrict the **filtered** result set:

```
GET /api/orders?status=pending&_limit=5&_offset=0
```

Returns the first 5 pending orders.

---

## Total Count

The REST API does not return a total-count header by default.  For total-count information, use the [GraphQL API](graphql_api.md), which exposes a `totalCount` field alongside every paginated result window.
