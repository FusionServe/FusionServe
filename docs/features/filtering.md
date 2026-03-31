# Filtering

## Overview

FusionServe supports two complementary filtering mechanisms on every `GET` list endpoint, allowing consumers to range from simple equality checks to complex multi-field boolean expressions — all via standard query-string parameters.

---

## Basic Equality Filtering

Any column name can be used as a query parameter to filter records by exact equality.

```
GET /api/users?role=admin&status=active
```

Internally, each provided parameter is added as a `WHERE` clause on the SQLAlchemy `select` statement:

```python
for k in condition.model_fields:
    if getattr(condition, k):
        statement = statement.where(getattr(orm_class, k) == getattr(condition, k))
```

**Behaviour:**

- Parameters that are absent or `None` are skipped — a missing parameter does not filter on that column.
- Multiple parameters are combined with `AND`.
- The allowed parameter names are constrained to the columns of the target table via the generated `get_input` Pydantic model, so unknown query parameters are ignored.

---

## OData Advanced Filtering

For multi-field expressions and inequality comparisons, the `_filter` query parameter accepts an **OData v4 filter expression** (with a leading underscore to avoid clashing with column names).

```
GET /api/users?_filter=(status eq 'active') and (age gt 18)
GET /api/orders?_filter=(total ge 100.00) and (region ne 'EU')
```

OData filter expressions are translated to native SQLAlchemy WHERE clauses via the [`odata-query`](https://pypi.org/project/odata-query/) library.

### Supported Operators

| Operator | Meaning | Example |
|---|---|---|
| `eq` | Equal | `status eq 'active'` |
| `ne` | Not equal | `region ne 'EU'` |
| `gt` | Greater than | `age gt 18` |
| `ge` | Greater than or equal | `total ge 100.00` |
| `lt` | Less than | `price lt 9.99` |
| `le` | Less than or equal | `score le 50` |
| `and` | Logical AND | `(a eq 1) and (b eq 2)` |
| `or` | Logical OR | `(a eq 1) or (a eq 2)` |
| `not` | Logical NOT | `not (status eq 'inactive')` |

### Expression Syntax

- String literals must be enclosed in **single quotes**: `name eq 'Alice'`
- Numeric literals are written without quotes: `age gt 18`
- Sub-expressions may be wrapped in parentheses for clarity
- Compound expressions use `and` / `or` to join clauses

### Validation

The filter value is validated by the [`AdvancedFilter`](../../src/fusionserve/models.py) Pydantic model using a regex pattern before the expression is passed to the OData parser.  If the OData library raises a parsing or field-resolution error, a `400 Bad Request` is returned:

```
400 Bad Request
{"detail": "Invalid filter: Unknown field 'nonexistent'"}
```

---

## Combining Both Filter Types

Basic equality filters and OData filters are applied **in sequence** and are cumulative (`AND` semantics):

```
GET /api/products?category=electronics&_filter=(price lt 500) and (in_stock eq true)
```

This returns products where:
- `category = 'electronics'` (basic filter), **and**
- `price < 500` (OData filter), **and**
- `in_stock = true` (OData filter)

---

## Parameter Reference

| Parameter | Type | Description |
|---|---|---|
| `<column_name>` | `string` | Exact-match filter on the named column |
| `_filter` | `string` | OData v4 filter expression |

Both parameters are optional.  Omitting them returns all records (subject to pagination limits).

---

## Implementation Notes

- Filtering is combined with pagination: the limit/offset is applied to the filtered result set.
- OData parsing happens after basic equality filtering has been appended to the statement, so both mechanisms operate on the same underlying query.
- The `_` prefix on `_filter` ensures it cannot collide with any real column name (PostgreSQL identifiers cannot start with `_` in the standard naming convention used by FusionServe's table requirements).
