# Pagination

## Overview

Every `GET` list endpoint in FusionServe supports **limit/offset pagination** via two reserved query parameters.  A configurable maximum page size prevents accidental or malicious retrieval of unbounded result sets.

---

## Query Parameters

| Parameter | Type | Default | Constraints | Description |
|---|---|---|---|---|
| `_limit` | `integer` | `50` | `>= 1` | Maximum number of records to return (default from `default_page_size`) |
| `_offset` | `integer` | `0` | `>= 0` | Number of records to skip before returning results |

The leading underscore prefix is intentional — it prevents collisions with column names used for [basic equality filtering](filtering.md).

### Example

```
GET /api/v1/users?_limit=10&_offset=20
```

Returns records 21–30 (zero-indexed).

---

## Maximum Page Size

The server-side maximum is controlled by the `max_page_size` configuration setting (default `1000`).  Requests that specify a `_limit` value above this ceiling are rejected with a validation error.

| Setting | Default | Description |
|---|---|---|
| `max_page_size` | `1000` | Absolute upper bound on `_limit` |

---

## How It Works

Pagination is implemented as a **Litestar dependency** using `advanced-alchemy`'s `LimitOffset` filter.  The dependency is created by `create_filter_dependencies()` (re-exported from `advanced_alchemy` via [`fusionserve.di`](../../src/fusionserve/di.py)) on each generated controller, seeded with the configured default page size:

```python
dependencies = create_filter_dependencies(
    {
        "pagination_type": "limit_offset",
        "pagination_size": settings.default_page_size,  # default 50
    }
)
```

The resulting `LimitOffset` filter is appended to the SQLAlchemy `SELECT` statement before execution (a `_limit` above `max_page_size` is rejected with `400 Bad Request`):

```python
statement = filters[0].append_to_statement(select(orm_class), orm_class)
```

---

## Combining with Filtering

Pagination and filtering are applied together.  The `_limit` / `_offset` parameters restrict the **filtered** result set:

```
GET /api/v1/orders?status=pending&_limit=5&_offset=0
```

Returns the first 5 pending orders.

---

## Total Count

The REST API does not return a total-count header by default.  For total-count information, use the [GraphQL API](graphql_api.md), which exposes a `totalCount` field alongside every paginated result window.
