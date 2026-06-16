"""Custom GraphQL list connections for FusionServe.

Replaces strawberry-orm's relay ``ORMListConnection`` (which requires
``relay.Node`` — hiding native ``id``/PK columns behind an opaque ``GlobalID``
and offering no composite-PK support) with a connection that:

* works with **composite primary keys**,
* leaves the native PK columns visible as ordinary fields,
* offers **cursor (keyset)** pagination (``first``/``after``/``last``/``before``)
  honouring the ``order`` argument, with the primary key appended as a stable
  **descending** tiebreaker (newest-first by default, since PKs are typically
  auto-increment integers or UUIDv7),
* offers **limit/offset** pagination,
* exposes both a relay-style shape (``edges { cursor node }``) and a flat
  ``nodes`` shape, plus ``pageInfo`` and ``totalCount``.

Cursor format: ``base64("<Type>:<v1|v2|…>")`` where ``v1..vn`` are the
percent-encoded values of the row's *effective sort key* — the columns named in
``order`` (in order) followed by the primary-key columns appended as
tiebreakers. With no ``order`` argument the key is just the PK, so the cursor is
``base64("<Type>:<pk1|pk2>")``. A ``\\x00`` sentinel encodes ``NULL``.

The resolver materialises through strawberry-orm's optimizer
(``optimize_query_nodes``) so nested relations are eager-loaded with no N+1, and
runs on the request's role-scoped session (RLS-consistent).

Note: this module intentionally does **not** use ``from __future__ import
annotations`` — the generic ``Edge[T]`` / ``Connection[T]`` field annotations
must remain real objects for Strawberry to resolve them when specialised.
"""

import base64
import inspect
import urllib.parse
from dataclasses import dataclass
from typing import Any

import strawberry
import strawberry_orm.optimizer.selections as _orm_selections
from pydantic import TypeAdapter
from sqlalchemy import Select, and_, asc, desc, false, func, or_, select
from strawberry.annotation import StrawberryAnnotation
from strawberry.types.arguments import StrawberryArgument
from strawberry_orm.optimizer.extension import optimize_query_nodes

from .config import settings

# Teach strawberry-orm's optimizer to descend our flat ``nodes`` wrapper (it
# already passes through relay ``edges``/``node``), so nested to-one relations
# selected via ``nodes { … }`` are eager-loaded instead of triggering an async
# lazy-load. Pinned by tests/test_graphql_orm_contract.py.
if "nodes" not in _orm_selections._RELAY_PASSTHROUGH_FIELDS:
    _orm_selections._RELAY_PASSTHROUGH_FIELDS = _orm_selections._RELAY_PASSTHROUGH_FIELDS | {"nodes"}

#: Cursor sentinel for a NULL key value (never produced by percent-encoding).
_NULL_TOKEN = "\x00"


@strawberry.type(description="Pagination metadata for a connection.")
class PageInfo:
    """Relay-style page metadata."""

    has_next_page: bool
    has_previous_page: bool
    start_cursor: str | None
    end_cursor: str | None


@strawberry.type(description="An edge: a node plus its pagination cursor.")
class Edge[T]:
    """A single connection edge."""

    cursor: str
    node: T


@strawberry.type(description="A paginated list of items.")
class Connection[T]:
    """A connection exposing both ``edges`` and a flat ``nodes`` list."""

    edges: list[Edge[T]]
    nodes: list[T]
    page_info: PageInfo
    total_count: int


# -- Cursor encoding -----------------------------------------------------------

_ADAPTERS: dict[Any, TypeAdapter] = {}


def _adapter(python_type: Any) -> TypeAdapter:
    adapter = _ADAPTERS.get(python_type)
    if adapter is None:
        adapter = TypeAdapter(python_type)
        _ADAPTERS[python_type] = adapter
    return adapter


def _encode_value(value: Any) -> str:
    if value is None:
        return _NULL_TOKEN
    rendered = value.isoformat() if hasattr(value, "isoformat") else str(value)
    return urllib.parse.quote(rendered, safe="")


def _decode_value(token: str, python_type: Any) -> Any:
    if token == _NULL_TOKEN:
        return None
    raw = urllib.parse.unquote(token)
    try:
        return _adapter(python_type).validate_python(raw)
    except Exception:
        return raw


def encode_cursor(type_name: str, values: list[Any]) -> str:
    """Encode ``base64("<type_name>:<v1|v2|…>")`` from a row's sort-key values."""
    body = f"{type_name}:" + "|".join(_encode_value(v) for v in values)
    return base64.b64encode(body.encode()).decode()


def decode_cursor(cursor: str, python_types: list[Any]) -> list[Any]:
    """Decode a cursor back to its sort-key values, cast to ``python_types``."""
    body = base64.b64decode(cursor).decode()
    _type_name, _, rest = body.partition(":")
    tokens = rest.split("|") if rest else []
    return [_decode_value(tok, pt) for tok, pt in zip(tokens, python_types, strict=False)]


# -- Sort key & keyset predicate ----------------------------------------------


@dataclass
class _KeySpec:
    """One column of the effective sort key."""

    name: str
    column: Any  # InstrumentedAttribute
    is_asc: bool
    python_type: Any


def _column_python_type(table, name: str) -> Any:
    try:
        return table.columns[name].type.python_type
    except KeyError, NotImplementedError:
        return str


def _effective_key(order_input: Any, orm_class: type) -> list[_KeySpec]:
    """Resolve the effective sort key: ``order`` scalar columns + PK tiebreakers.

    Only scalar ``field`` ordering is used for the keyset key; relation
    (``object``) ordering is ignored here (honoured only in offset mode).
    """
    table = orm_class.__table__
    specs: list[_KeySpec] = []
    seen: set[str] = set()
    if order_input not in (None, strawberry.UNSET):
        entries = order_input if isinstance(order_input, list) else [order_input]
        for entry in entries:
            field_val = getattr(entry, "field", None)
            if field_val is None or field_val is strawberry.UNSET:
                continue
            for col_name in field_val.__class__.__dataclass_fields__:
                direction = getattr(field_val, col_name)
                if direction is strawberry.UNSET or direction is None:
                    continue
                column = getattr(orm_class, col_name, None)
                if column is None:
                    continue
                dir_value = direction.value if hasattr(direction, "value") else str(direction)
                specs.append(
                    _KeySpec(col_name, column, dir_value.startswith("ASC"), _column_python_type(table, col_name))
                )
                seen.add(col_name)
    # Append the PK as the tiebreaker, **descending**: PKs are typically
    # auto-increment integers or UUIDv7, so DESC surfaces newest-first by
    # default (no `order` → PK DESC) and breaks ties newest-first under an
    # explicit order.
    for pk in table.primary_key.columns:
        if pk.name in seen:
            continue
        specs.append(_KeySpec(pk.name, getattr(orm_class, pk.name), False, _column_python_type(table, pk.name)))
        seen.add(pk.name)
    return specs


def _keyset_predicate(specs: list[_KeySpec], values: list[Any], *, before: bool) -> Any:
    """Build the lexicographic OR-expansion for keyset pagination.

    Honours each column's direction (``>`` for ASC, ``<`` for DESC), reversed for
    ``before``. NULL boundary values are matched with ``IS NULL`` for equality;
    a strict comparison against a NULL boundary yields no rows (documented
    nullable-order caveat — PK tiebreakers are non-null).
    """
    branches: list[Any] = []
    for i, spec in enumerate(specs):
        equals = []
        for j in range(i):
            ev = values[j]
            equals.append(specs[j].column.is_(None) if ev is None else specs[j].column == ev)
        ascending = spec.is_asc != before  # XOR: reverse comparator for `before`
        val = values[i]
        if val is None:
            strict = false()
        elif ascending:
            strict = spec.column > val
        else:
            strict = spec.column < val
        branches.append(and_(*equals, strict))
    return or_(*branches)


# -- Field builder -------------------------------------------------------------


async def materialize(stmt: Select, info: Any) -> list[Any]:
    """Eager-load per the selection set and execute on the role-scoped session.

    Routes the statement through strawberry-orm's optimizer so the GraphQL
    selection set drives ``selectinload``/``joinedload`` (no N+1 / no async
    lazy-load), executing on the request's role-scoped session. Shared by the
    connection resolvers and the primary-key lookup field.
    """
    result = optimize_query_nodes(stmt, info)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, Select):  # no optimizer configured — execute directly
        result = (await info.context.session.execute(result)).scalars().unique().all()
    return list(result)


def build_connection_field(
    orm: Any,
    orm_class: type,
    gql_type: type,
    filter_type: type | None,
    order_type: type | None,
    type_name: str,
    *,
    description: str | None = None,
):
    """Build a ``strawberry.field`` resolving to a :class:`Connection` of ``gql_type``.

    Arguments ``filter``, ``order``, ``first``/``after``/``last``/``before`` and
    ``limit``/``offset`` are wired onto the field.     Cursor (keyset) and
    limit/offset pagination are mutually exclusive.
    """

    async def resolver(
        info: strawberry.Info,
        filter: Any = strawberry.UNSET,
        order: Any = strawberry.UNSET,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> Any:
        cursor_mode = any(v is not None for v in (first, after, last, before))
        offset_mode = limit is not None or offset is not None
        if cursor_mode and offset_mode:
            raise ValueError("Use either cursor pagination (first/after/last/before) or limit/offset, not both.")

        base = select(orm_class)
        if filter:
            base = orm.backend.apply_filters(base, filter, orm_class)

        session = info.context.session
        total_count = (await session.execute(select(func.count()).select_from(base.subquery()))).scalar_one()

        specs = _effective_key(order, orm_class)
        forward_clauses = [asc(s.column) if s.is_asc else desc(s.column) for s in specs]

        if offset_mode:
            page_size = limit if limit is not None else settings.default_page_size
            skip = offset or 0
            # Honour full ordering (incl. relation `object` ordering) in offset mode.
            ordered = base
            if order not in (None, strawberry.UNSET):
                ordered = orm.backend.apply_ordering(ordered, order, orm_class)
            else:
                ordered = ordered.order_by(*forward_clauses)
            stmt = ordered.offset(skip).limit(page_size + 1)
            rows = await materialize(stmt, info)
            has_next = len(rows) > page_size
            rows = rows[:page_size]
            has_previous = skip > 0
        else:
            backward = last is not None or (before is not None and first is None)
            size = (last if last is not None else first) or settings.default_page_size
            stmt = base
            key_types = [s.python_type for s in specs]
            if after is not None:
                stmt = stmt.where(_keyset_predicate(specs, decode_cursor(after, key_types), before=False))
            if before is not None:
                stmt = stmt.where(_keyset_predicate(specs, decode_cursor(before, key_types), before=True))
            page_clauses = (
                [desc(s.column) if s.is_asc else asc(s.column) for s in specs] if backward else forward_clauses
            )
            stmt = stmt.order_by(*page_clauses).limit(size + 1)
            rows = await materialize(stmt, info)
            extra = len(rows) > size
            rows = rows[:size]
            if backward:
                rows.reverse()
                has_previous = extra
                has_next = before is not None
            else:
                has_next = extra
                has_previous = after is not None

        edges = [Edge(cursor=encode_cursor(type_name, [getattr(row, s.name) for s in specs]), node=row) for row in rows]
        page_info = PageInfo(
            has_next_page=has_next,
            has_previous_page=has_previous,
            start_cursor=edges[0].cursor if edges else None,
            end_cursor=edges[-1].cursor if edges else None,
        )
        return Connection(edges=edges, nodes=list(rows), page_info=page_info, total_count=total_count)

    field = strawberry.field(resolver=resolver, description=description)
    field.type_annotation = StrawberryAnnotation(Connection[gql_type])

    arguments: list[StrawberryArgument] = []
    if filter_type is not None:
        arguments.append(
            StrawberryArgument("filter", None, StrawberryAnnotation(filter_type | None), default=strawberry.UNSET)
        )
    if order_type is not None:
        arguments.append(
            StrawberryArgument("order", None, StrawberryAnnotation(list[order_type] | None), default=strawberry.UNSET)
        )
    for name in ("first", "last", "limit", "offset"):
        arguments.append(StrawberryArgument(name, None, StrawberryAnnotation(int | None), default=None))
    for name in ("after", "before"):
        arguments.append(StrawberryArgument(name, None, StrawberryAnnotation(str | None), default=None))
    field.base_resolver.arguments = arguments
    return field
