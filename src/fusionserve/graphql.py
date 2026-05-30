"""GraphQL schema and resolver generation for FusionServe.

This module dynamically builds a Strawberry GraphQL schema from SQLAlchemy
automap models. For each registered table it creates two query fields: a
paginated list resolver and a primary-key lookup resolver. The schema is
then exposed via a Litestar-compatible GraphQL controller.
"""

import logging
from contextlib import asynccontextmanager
from typing import Any, TypeVar

# TODO: restore from litestar import Request: need bettere understanding namespace clash.
import litestar
import litestar.datastructures
import strawberry
from pydantic.alias_generators import to_pascal
from sqlalchemy import Column, Table, and_, delete, func, insert, not_, or_, select, text, update
from sqlalchemy.orm import DeclarativeMeta
from sqlalchemy.sql.expression import ColumnElement
from strawberry.annotation import StrawberryAnnotation
from strawberry.extensions import QueryDepthLimiter
from strawberry.litestar import (
    BaseContext,
    HTTPContextType,
    WebSocketContextType,
    make_graphql_controller,
)
from strawberry.types.arguments import StrawberryArgument
from strawberry.utils.str_converters import to_camel_case, to_snake_case
from strawberry_sqlalchemy_mapper import StrawberrySQLAlchemyLoader, StrawberrySQLAlchemyMapper

from . import auth
from .config import settings
from .models import (
    COMPARISON_TYPE_MAP,
    FunctionInfo,
    FunctionReturnKind,
    Introspection,
    RecordNotFoundError,
    ResolverType,
    SmartComment,
    SortDirection,
)
from .persistence import _scalar_name_from_constraint, apply_load_only, async_session, inflect, set_role

_logger = logging.getLogger(settings.app_name)

Item = TypeVar("Item")

# Maximum recursion depth for nested where clauses to prevent abuse.
_MAX_WHERE_DEPTH = 10

# Maps comparison operator names to SQLAlchemy expression builders.
OPERATOR_MAP: dict[str, object] = {
    "eq": lambda col, val: col == val,
    "neq": lambda col, val: col != val,
    "gt": lambda col, val: col > val,
    "gte": lambda col, val: col >= val,
    "lt": lambda col, val: col < val,
    "lte": lambda col, val: col <= val,
    "in_list": lambda col, val: col.in_(val),
    "not_in_list": lambda col, val: col.notin_(val),
    "like": lambda col, val: col.like(val),
    "ilike": lambda col, val: col.ilike(val),
    "is_null": lambda col, val: col.is_(None) if val else col.isnot(None),
}

# Field names reserved for boolean combinators in Where input types.
COMBINATOR_FIELDS = frozenset({"_and", "_or", "_not"})


@strawberry.type
class PaginationWindow[Item]:
    """A generic paginated response wrapper for GraphQL list queries.

    Attributes:
        nodes: The list of items in this pagination window.
        total_count: Total number of items in the filtered dataset.
    """

    nodes: list[Item] = strawberry.field(description="The list of items in this pagination window.")
    total_count: int = strawberry.field(description="Total number of items in the filtered dataset.")


class CustomContext(BaseContext, kw_only=True):
    """Custom Strawberry context carrying the SQLAlchemy relationship loader.

    Attributes:
        sqlalchemy_loader: The dataloader used by the strawberry-sqlalchemy
            mapper to resolve relationship fields. Configured with an
            ``async_bind_factory`` that opens a fresh session per batch and
            applies the authenticated user's PostgreSQL role so row-level
            security stays consistent with the CRUD resolvers.
    """

    sqlalchemy_loader: StrawberrySQLAlchemyLoader


class CustomHTTPContextType(HTTPContextType, CustomContext):
    """HTTP-specific context type combining Litestar HTTP context with custom context.

    Attributes:
        request: The typed Litestar HTTP request object.
    """

    request: litestar.Request[auth.User, Any, litestar.datastructures.State]


class CustomWSContextType(WebSocketContextType, CustomContext):
    """WebSocket-specific context type combining Litestar WS context with custom context.

    Attributes:
        socket: The typed Litestar WebSocket connection object.
    """

    socket: litestar.WebSocket[auth.User, Any, litestar.datastructures.State]


async def custom_context_getter(request: litestar.Request) -> CustomContext:
    """Create a custom Strawberry context for each GraphQL request.

    The SQLAlchemy relationship loader receives an ``async_bind_factory`` that
    opens a fresh async session for each batch of related-row fetches and
    applies the request user's PostgreSQL role before yielding, so row-level
    security stays consistent with the CRUD resolvers.

    Args:
        request: The incoming Litestar HTTP request.

    Returns:
        A ``CustomContext`` wrapping a configured
        :class:`StrawberrySQLAlchemyLoader`.
    """

    @asynccontextmanager
    async def loader_bind():
        async with async_session() as loader_session:
            await set_role(loader_session, request.user)
            yield loader_session

    return CustomContext(
        sqlalchemy_loader=StrawberrySQLAlchemyLoader(async_bind_factory=loader_bind),
    )


@strawberry.experimental.pydantic.type(model=auth.User, all_fields=True, description="The authenticated user from JWT")
class JWTUser:
    pass


def _set_resolver_arguments(field, arguments: list[StrawberryArgument]) -> None:
    """Replace a Strawberry field's resolver argument list in place.

    Strawberry derives a resolver's GraphQL arguments from its Python
    signature. When the argument types are dynamically generated per table
    (e.g. ``UsersWhere``), the simplest reliable hook is to reassign
    ``base_resolver.arguments`` after the field has been constructed. This
    helper centralises that pattern so call sites are easy to spot.

    Args:
        field: The Strawberry field returned by ``strawberry.field`` or
            ``strawberry.mutation``.
        arguments: The new argument list to install.
    """
    field.base_resolver.arguments = arguments


def _column_default(column: Column, default: object) -> object:
    """Return ``default`` wrapped in a ``strawberry.field`` if the column has a comment.

    Looks up the column's smart-comment via :meth:`SmartComment.from_object`
    and, when the resulting ``content`` is non-empty, wraps the default in a
    Strawberry field carrying that description. Otherwise the bare default is
    returned unchanged so columns without comments don't carry empty
    descriptions.

    Args:
        column: The SQLAlchemy ``Column`` whose comment supplies the
            description.
        default: The default value to attach to the resulting input field
            (typically ``strawberry.UNSET``).

    Returns:
        Either ``default`` unchanged, or a ``strawberry.field(...)`` carrying
        the column's smart-comment content as its GraphQL description.
    """
    content = SmartComment.from_object(column).content
    if not content:
        return default
    return strawberry.field(default=default, description=content)


def _set_field_descriptions(gql_type: type, table: Table) -> None:
    """Copy column comments onto the matching Strawberry output-field descriptions.

    Walks ``gql_type.__strawberry_definition__.fields`` and, for every field
    whose ``python_name`` matches a column on ``table``, sets the field's
    GraphQL description from :meth:`SmartComment.from_object`. Relationship
    fields and other non-column fields are left untouched.

    Args:
        gql_type: The Strawberry-decorated output type returned by
            ``mapper.type(orm_class)(...)``.
        table: The SQLAlchemy ``Table`` whose column comments source the
            descriptions.
    """
    for column in table.columns:
        field = gql_type.__strawberry_definition__.get_field(column.name)
        if field is None:
            continue
        content = SmartComment.from_object(column).content
        if content:
            field.description = content


def _rename_fk_fields(gql_type: type, table: Table) -> None:
    """Rename Relationship fields in singular form

    Args:
        gql_type: The Strawberry-decorated output type returned by
            ``mapper.type(orm_class)(...)``.
        table: The SQLAlchemy ``Table`` whose foreign keys are used to identify relationship fields.
    """
    for fk in table.foreign_key_constraints:
        field = gql_type.__strawberry_definition__.get_field(fk.referred_table.name)
        if field:
            new_name = to_camel_case(
                inflect.singular_noun(fk.referred_table.name)
                if inflect.singular_noun(fk.referred_table.name)
                else fk.referred_table.name
            )
            _logger.debug("Renaming relationship field %s in table %s to %s", field.name, table.name, new_name)
            field.graphql_name = new_name
        field = gql_type.__strawberry_definition__.get_field(_scalar_name_from_constraint(fk, fk.table.name))
        if field:
            new_name = to_camel_case(
                inflect.singular_noun(_scalar_name_from_constraint(fk, fk.table.name))
                if inflect.singular_noun(_scalar_name_from_constraint(fk, fk.table.name))
                else _scalar_name_from_constraint(fk, fk.table.name)
            )
            _logger.debug("Renaming relationship field %s in table %s to %s", field.name, table.name, new_name)
            field.graphql_name = new_name


def columns_from_selections(
    selections: list[strawberry.types.nodes.Selection], orm_class: DeclarativeMeta
) -> list[str]:
    """Extract column names from a Strawberry GraphQL selection set.

    Recursively traverses the selection AST and collects snake_case
    column names that exist on the given ORM table. Handles both
    ``SelectedField`` and ``FragmentSpread`` node types.

    Foreign-key columns on ``orm_class`` are always appended to the result,
    regardless of whether the client selected them. ``load_only`` defers
    every non-PK column it isn't told to keep, and the strawberry-sqlalchemy
    mapper's relationship loader needs the FK value on the parent row to
    resolve to-one relationships — without this, queries like
    ``{ order { user { name } } }`` lose access to ``order.user_id`` and the
    related lookup silently fails.

    Args:
        selections: The list of Strawberry selection nodes to inspect.
        orm_class: The SQLAlchemy ORM class whose ``__table__.columns``
            are used to validate column existence and to enumerate FKs.

    Returns:
        A list of column name strings in snake_case containing every
        explicitly-selected scalar column plus every foreign-key column on
        ``orm_class``.
    """
    selected_columns: list[str] = []
    for selection in selections:
        if (
            isinstance(selection, strawberry.types.nodes.SelectedField)
            and to_snake_case(selection.name) in orm_class.__table__.columns
        ):
            selected_columns.append(to_snake_case(selection.name))
        if isinstance(selection, strawberry.types.nodes.FragmentSpread):
            cols = [to_snake_case(col.name) for col in selection.selections]
            selected_columns.extend(cols)
        if len(selection.selections) > 0:
            # recursively get nested selections
            selected_columns.extend(columns_from_selections(selection.selections, orm_class))
    # Always include FK columns so the mapper's relationship loader can
    # resolve to-one relationships even when the FK wasn't selected.
    for column in orm_class.__table__.columns:
        if column.foreign_keys and column.name not in selected_columns:
            selected_columns.append(column.name)
    return selected_columns


def create_order_by_input(table: Table) -> type:
    """Dynamically create a Strawberry input type for ordering a table's columns.

    Generates a class named ``{PascalTableName}OrderBy`` with one optional
    ``SortDirection`` field per column. Unset fields default to
    ``strawberry.UNSET`` so that omitted columns produce no ordering clause.

    Args:
        table: The SQLAlchemy ``Table`` whose columns drive the input fields.

    Returns:
        A Strawberry ``@input``-decorated class.
    """
    annotations: dict[str, type] = {}
    defaults: dict[str, object] = {}
    for column in table.columns:
        annotations[column.name] = SortDirection | None
        defaults[column.name] = _column_default(column, strawberry.UNSET)

    class_name = f"{to_pascal(table.name)}OrderBy"
    cls = type(class_name, (object,), {"__annotations__": annotations, **defaults})
    return strawberry.input(cls)


def create_where_input(table: Table) -> type:
    """Dynamically create a Strawberry input type for filtering a table's columns.

    Generates a class named ``{PascalTableName}Where`` with one optional
    typed comparison field per column (e.g. ``StringComparisonExp`` for
    string columns) and self-referential boolean combinators
    (``_and``, ``_or``, ``_not``).

    Args:
        table: The SQLAlchemy ``Table`` whose columns drive the input fields.

    Returns:
        A Strawberry ``@input``-decorated class with per-column comparison
        fields and boolean combinator fields.
    """
    annotations: dict[str, type] = {}
    defaults: dict[str, object] = {}
    for column in table.columns:
        try:
            python_type = column.type.python_type
        except NotImplementedError:
            python_type = str
        comparison_type = COMPARISON_TYPE_MAP.get(python_type, COMPARISON_TYPE_MAP[str])
        annotations[column.name] = comparison_type | None
        defaults[column.name] = _column_default(column, strawberry.UNSET)

    class_name = f"{to_pascal(table.name)}Where"
    cls = type(class_name, (object,), {"__annotations__": annotations, **defaults})

    # Add self-referential boolean combinators. The class object is concrete
    # by this point, so list[cls] | None resolves without forward refs.
    cls.__annotations__["_and"] = list[cls] | None
    cls.__annotations__["_or"] = list[cls] | None
    cls.__annotations__["_not"] = cls | None
    cls._and = strawberry.UNSET  # type: ignore[attr-defined]
    cls._or = strawberry.UNSET  # type: ignore[attr-defined]
    cls._not = strawberry.UNSET  # type: ignore[attr-defined]
    return strawberry.input(cls)


def create_input_type(table: Table, mapper: StrawberrySQLAlchemyMapper) -> type:
    """Dynamically create a Strawberry input type for creating a new record.

    Generates a class named ``{PascalTableName}Input`` with one optional
    field per column. Unset fields default to ``strawberry.UNSET`` so that
    omitted columns can be handled with database defaults or nullable
    constraints.

    Args:
        table: The SQLAlchemy ``Table`` whose columns drive the input fields.
        mapper: The ``StrawberrySQLAlchemyMapper`` used to translate
            SQLAlchemy column types to Strawberry-compatible Python types.

    Returns:
        A Strawberry ``@input``-decorated class.
    """
    annotations: dict[str, type] = {}
    defaults: dict[str, object] = {}
    for column in table.columns:
        try:
            column_type = mapper._convert_column_to_strawberry_type(column)
        except NotImplementedError:
            column_type = str
        annotations[column.name] = column_type
        defaults[column.name] = _column_default(column, strawberry.UNSET)
        if column.server_default is not None:
            annotations[column.name] = column_type | None

    class_name = f"{to_pascal(table.name)}Input"
    cls = type(class_name, (object,), {"__annotations__": annotations, **defaults})
    return strawberry.input(cls)


def patch_input_type(table: Table, mapper: StrawberrySQLAlchemyMapper) -> type:
    """Dynamically create a Strawberry input type for patching an existing record.

    Generates a class named ``{PascalTableName}Patch`` with one optional
    field per non-primary-key column. All fields default to
    ``strawberry.UNSET`` so that omitted columns are left untouched during
    the update. Primary key columns are excluded because they identify the
    target record and are passed as separate resolver arguments.

    Args:
        table: The SQLAlchemy ``Table`` whose columns drive the input fields.
        mapper: The ``StrawberrySQLAlchemyMapper`` used to translate
            SQLAlchemy column types to Strawberry-compatible Python types.

    Returns:
        A Strawberry ``@input``-decorated class.
    """
    pk_names = set(table.primary_key.columns.keys())
    annotations: dict[str, type] = {}
    defaults: dict[str, object] = {}
    for column in table.columns:
        if column.name in pk_names:
            continue
        try:
            column_type = mapper._convert_column_to_strawberry_type(column)
        except NotImplementedError:
            column_type = str
        annotations[column.name] = column_type | None
        defaults[column.name] = _column_default(column, strawberry.UNSET)

    class_name = f"{to_pascal(table.name)}Patch"
    cls = type(class_name, (object,), {"__annotations__": annotations, **defaults})
    return strawberry.input(cls)


def apply_order_by(statement: select, orm_class: DeclarativeMeta, order_by_input: object) -> select:
    """Apply ordering clauses from a Strawberry order_by input to a SQLAlchemy statement.

    Iterates over the fields of the input object and, for each field that
    is not ``UNSET`` or ``None``, appends the corresponding SQLAlchemy
    ``.order_by()`` clause.

    Args:
        statement: The SQLAlchemy ``Select`` statement to extend.
        orm_class: The ORM class providing column references.
        order_by_input: An instance of a dynamically created ``OrderBy``
            input type.

    Returns:
        The statement with ``.order_by()`` clauses appended.
    """
    for field_name in order_by_input.__class__.__annotations__:
        direction = getattr(order_by_input, field_name)
        if direction is strawberry.UNSET or direction is None:
            continue
        column = getattr(orm_class, field_name)
        match direction:
            case SortDirection.ASC:
                clause = column.asc()
            case SortDirection.ASC_NULLS_FIRST:
                clause = column.asc().nulls_first()
            case SortDirection.ASC_NULLS_LAST:
                clause = column.asc().nulls_last()
            case SortDirection.DESC:
                clause = column.desc()
            case SortDirection.DESC_NULLS_FIRST:
                clause = column.desc().nulls_first()
            case SortDirection.DESC_NULLS_LAST:
                clause = column.desc().nulls_last()
            case _:
                continue
        statement = statement.order_by(clause)
    return statement


def apply_where(orm_class: DeclarativeMeta, where_input: object, _depth: int = 0) -> ColumnElement | None:
    """Recursively convert a Where input instance into a SQLAlchemy filter expression.

    Handles per-column typed comparison operators and boolean combinators
    (``_and``, ``_or``, ``_not``). Top-level fields within a single
    ``where`` object are implicitly ANDed.

    Args:
        orm_class: The ORM class providing column references.
        where_input: An instance of a dynamically created ``Where``
            input type.
        _depth: Current recursion depth (guarded by ``_MAX_WHERE_DEPTH``).

    Returns:
        A SQLAlchemy ``ColumnElement`` representing the combined filter,
        or ``None`` if no conditions were specified.

    Raises:
        ValueError: If the recursion depth exceeds ``_MAX_WHERE_DEPTH``.
    """
    if _depth > _MAX_WHERE_DEPTH:
        msg = f"where filter nesting exceeds maximum depth of {_MAX_WHERE_DEPTH}"
        raise ValueError(msg)

    conditions: list[ColumnElement] = []

    for field_name in where_input.__class__.__annotations__:
        value = getattr(where_input, field_name)
        if value is strawberry.UNSET or value is None:
            continue

        # --- Boolean combinators ---
        if field_name == "_and":
            sub = [apply_where(orm_class, item, _depth + 1) for item in value]
            sub = [s for s in sub if s is not None]
            if sub:
                conditions.append(and_(*sub))
            continue

        if field_name == "_or":
            sub = [apply_where(orm_class, item, _depth + 1) for item in value]
            sub = [s for s in sub if s is not None]
            if sub:
                conditions.append(or_(*sub))
            continue

        if field_name == "_not":
            sub = apply_where(orm_class, value, _depth + 1)
            if sub is not None:
                conditions.append(not_(sub))
            continue

        # --- Column comparison field ---
        if field_name in COMBINATOR_FIELDS:
            continue

        column = getattr(orm_class, field_name, None)
        if column is None:
            continue

        comparison = value  # the typed comparison input instance
        for op_name in comparison.__class__.__annotations__:
            op_value = getattr(comparison, op_name)
            if op_value is strawberry.UNSET or (op_value is None and op_name != "is_null"):
                continue
            builder = OPERATOR_MAP.get(op_name)
            if builder is not None:
                conditions.append(builder(column, op_value))

    if not conditions:
        return None
    return and_(*conditions)


def resolver_factory(
    orm_class: DeclarativeMeta,
    gql_type,
    resolver_type: ResolverType,
    mapper: StrawberrySQLAlchemyMapper,
):
    """Create a Strawberry resolver function for a given table and resolver type.

    Returns one of the eight resolver flavours (``LIST``, ``PK``, ``CREATE``,
    ``CREATE_MANY``, ``UPDATE``, ``UPDATE_MANY``, ``DELETE``, ``DELETE_MANY``).
    All resolvers open an independent async session and apply the request's
    PostgreSQL role before executing.

    Args:
        orm_class: The SQLAlchemy ORM class representing the underlying table.
        gql_type: The Strawberry GraphQL type mapped from the ORM class.
        resolver_type: Which resolver flavour to build.
        mapper: The ``StrawberrySQLAlchemyMapper`` instance used to derive
            create/patch input types.

    Returns:
        An async resolver function suitable for
        ``strawberry.field(resolver=...)`` or ``strawberry.mutation(...)``.
    """

    async def list_resolver(
        info: strawberry.Info[CustomHTTPContextType, None],
        limit: int = settings.default_page_size,
        offset: int = 0,
        order_by: create_order_by_input(orm_class.__table__) | None = None,  # type: ignore
        where: create_where_input(orm_class.__table__) | None = None,  # type: ignore
    ) -> PaginationWindow[gql_type]:  # type: ignore
        """Resolve a paginated list of records for the table.

        Args:
            info: The Strawberry resolver info containing the selection set
                and custom context.
            limit: Maximum number of records to return. Must be a positive
                integer no greater than ``settings.max_page_size``.
            offset: Number of records to skip before returning results.
                Must be non-negative.
            order_by: Optional per-column ordering input (dynamically typed
                per table).
            where: Optional per-column filter input with boolean combinators
                (dynamically typed per table).

        Returns:
            A ``PaginationWindow`` containing the matching nodes and
            total count.

        Raises:
            ValueError: If ``limit`` or ``offset`` are out of bounds.
        """
        if limit <= 0:
            raise ValueError("limit must be a positive integer")
        if limit > settings.max_page_size:
            raise ValueError(f"limit {limit} exceeds max_page_size {settings.max_page_size}")
        if offset < 0:
            raise ValueError("offset must be non-negative")
        statement = select(orm_class, func.count().over().label("total_count"))
        if where is not None:
            condition = apply_where(orm_class, where)
            if condition is not None:
                statement = statement.where(condition)
        statement = statement.limit(limit).offset(offset)
        if order_by is not None:
            statement = apply_order_by(statement, orm_class, order_by)
        # the resolver is called for each field, so `selected_fields[0]` is always set
        selected_columns = columns_from_selections(info.selected_fields[0].selections, orm_class)
        statement = apply_load_only(statement, orm_class, selected_columns)
        # if the users requests more than one top level field we run concurrently in a thread.
        # the context session is managed at the request level so we need a new one here
        async with async_session() as session:
            await set_role(session, info.context.request.user)
            rows = (await session.execute(statement)).all()
        if rows:
            return PaginationWindow[gql_type](
                nodes=[row[0] for row in rows if row[0] is not None],
                total_count=rows[0][1],
            )
        return PaginationWindow[gql_type](nodes=[], total_count=0)

    async def pk_resolver(info: strawberry.Info[CustomHTTPContextType, None], **kwids: object) -> gql_type:  # type: ignore
        """Resolve a single record by its primary key(s).

        Args:
            info: The Strawberry resolver info containing the selection set
                and custom context.
            **kwids: Primary key column name/value pairs used to identify
                the record.

        Returns:
            The matching ORM instance mapped to the GraphQL type.

        Raises:
            RecordNotFoundError: If no record is found for the given primary key(s).
        """
        statement = select(orm_class)
        for key, id in kwids.items():
            statement = statement.where(getattr(orm_class, key) == id)
        # the resolver is called for each field, so `selected_fields[0]` is always set
        selected_columns = columns_from_selections(info.selected_fields[0].selections, orm_class)
        statement = apply_load_only(statement, orm_class, selected_columns)
        # if the users requests more than one top level field we run concurrently in a thread.
        # the context session is managed at the request level so we need a new one here
        async with async_session() as session:
            await set_role(session, info.context.request.user)
            result = (await session.execute(statement)).scalar_one_or_none()
        if result is None:
            raise RecordNotFoundError(f"No {orm_class.__table__.name} record matches {kwids}")
        return result

    async def create_resolver(
        info: strawberry.Info[CustomHTTPContextType, None],
        input: create_input_type(orm_class.__table__, mapper),  # type: ignore
    ) -> gql_type:  # type: ignore
        """Create a new record with the given field values.

        Args:
            info: The Strawberry resolver info containing the selection set
                and custom context.
            **input: Column name/value pairs used to populate the new record.

        Returns:
            The newly created ORM instance mapped to the GraphQL type.
        """
        async with async_session() as session:
            await set_role(session, info.context.request.user)
            instance = orm_class(**{k: v for k, v in vars(input).items() if v is not strawberry.UNSET})
            session.add(instance)
            await session.commit()
            await session.refresh(instance)
        return instance

    async def create_many_resolver(
        info: strawberry.Info[CustomHTTPContextType, None],
        inputs: list[create_input_type(orm_class.__table__, mapper)],  # type: ignore
    ) -> list[gql_type]:  # type: ignore
        """Insert many records in a single statement.

        Issues a single ``INSERT ... VALUES (...), (...) RETURNING *`` so the
        created rows can be returned without a separate ``SELECT`` and without
        N round-trips. The whole insert is atomic — any constraint violation
        rolls back every row.

        Args:
            info: The Strawberry resolver info containing the selection set
                and custom context.
            inputs: List of column-name/value sets used to populate the new
                records. Fields left as ``strawberry.UNSET`` fall back to the
                column's default or NULL.

        Returns:
            The newly-created ORM instances mapped to the GraphQL type, in
            input order.

        Raises:
            ValueError: If ``inputs`` is empty.
        """
        if not inputs:
            raise ValueError("inputs must contain at least one record to create")
        rows = [{k: v for k, v in vars(item).items() if v is not strawberry.UNSET} for item in inputs]
        statement = insert(orm_class).values(rows).returning(orm_class)
        async with async_session() as session:
            await set_role(session, info.context.request.user)
            result = (await session.execute(statement)).scalars().all()
            await session.commit()
        return list(result)

    async def update_resolver(
        info: strawberry.Info[CustomHTTPContextType, None],
        patch: patch_input_type(orm_class.__table__, mapper),  # type: ignore
        **kwids: object,
    ) -> gql_type:  # type: ignore
        """Update an existing record with the given field values.

        Args:
            info: The Strawberry resolver info containing the selection set
                and custom context.
            patch: Column name/value pairs used to update the record. Fields
                left as ``strawberry.UNSET`` are not modified.
            **kwids: Primary key column name/value pairs used to identify
                the record.

        Returns:
            The updated ORM instance mapped to the GraphQL type.

        Raises:
            RecordNotFoundError: If no record is found for the given primary key(s).
        """
        statement = select(orm_class)
        for key, id in kwids.items():
            statement = statement.where(getattr(orm_class, key) == id)
        async with async_session() as session:
            await set_role(session, info.context.request.user)
            result = (await session.execute(statement)).scalar_one_or_none()
            if result is None:
                raise RecordNotFoundError(f"No {orm_class.__table__.name} record matches {kwids}")
            for key, value in vars(patch).items():
                if value is strawberry.UNSET:
                    continue
                setattr(result, key, value)
            await session.commit()
            await session.refresh(result)
        return result

    async def update_many_resolver(
        info: strawberry.Info[CustomHTTPContextType, None],
        patch: patch_input_type(orm_class.__table__, mapper),  # type: ignore
        where: create_where_input(orm_class.__table__),  # type: ignore
    ) -> list[gql_type]:  # type: ignore
        """Apply the same patch to every row matching a where condition.

        Issues a single ``UPDATE ... WHERE ... RETURNING *`` statement so the
        affected rows can be returned to the caller without a separate
        ``SELECT``.

        Args:
            info: The Strawberry resolver info containing the selection set
                and custom context.
            patch: Column name/value pairs applied to every matching row.
                Fields left as ``strawberry.UNSET`` are not modified.
            where: Filter input used to select the rows to update. An empty
                or fully-UNSET ``where`` is rejected to prevent accidental
                table-wide updates; pass explicit conditions to target all
                rows.

        Returns:
            The list of updated ORM instances as they exist after the update.

        Raises:
            ValueError: If ``patch`` contains no set fields, or if ``where``
                resolves to no filter condition.
        """
        patch_values = {k: v for k, v in vars(patch).items() if v is not strawberry.UNSET}
        if not patch_values:
            msg = "patch must contain at least one field to update"
            raise ValueError(msg)
        condition = apply_where(orm_class, where)
        if condition is None:
            msg = "where must contain at least one filter condition for update_many"
            raise ValueError(msg)
        statement = (
            update(orm_class)
            .where(condition)
            .values(**patch_values)
            .returning(orm_class)
            .execution_options(synchronize_session=None)
        )
        async with async_session() as session:
            await set_role(session, info.context.request.user)
            rows = (await session.execute(statement)).scalars().all()
            await session.commit()
        return list(rows)

    async def delete_resolver(info: strawberry.Info[CustomHTTPContextType, None], **kwids: object) -> gql_type:  # type: ignore
        """Delete an existing record identified by its primary key(s).

        Issues a single ``DELETE ... RETURNING *`` statement so the deleted
        row can be returned to the caller without a separate ``SELECT``.

        Args:
            info: The Strawberry resolver info containing the selection set
                and custom context.
            **kwids: Primary key column name/value pairs used to identify
                the record.

        Returns:
            The ORM instance representing the row as it existed immediately
            before deletion.

        Raises:
            RecordNotFoundError: If no record is found for the given primary key(s).
        """
        statement = (
            delete(orm_class)
            .where(*[getattr(orm_class, key) == value for key, value in kwids.items()])
            .returning(orm_class)
            .execution_options(synchronize_session=None)
        )
        async with async_session() as session:
            await set_role(session, info.context.request.user)
            result = (await session.execute(statement)).scalar_one_or_none()
            if result is None:
                raise RecordNotFoundError(f"No {orm_class.__table__.name} record matches {kwids}")
            await session.commit()
        return result

    async def delete_many_resolver(
        info: strawberry.Info[CustomHTTPContextType, None],
        where: create_where_input(orm_class.__table__),  # type: ignore
    ) -> list[gql_type]:  # type: ignore
        """Delete every row matching a where condition.

        Issues a single ``DELETE ... WHERE ... RETURNING *`` statement so the
        deleted rows can be returned to the caller without a separate
        ``SELECT``.

        Args:
            info: The Strawberry resolver info containing the selection set
                and custom context.
            where: Filter input used to select the rows to delete. An empty
                or fully-UNSET ``where`` is rejected to prevent accidental
                table-wide deletes; pass explicit conditions to target all
                rows.

        Returns:
            The list of ORM instances representing the rows as they existed
            immediately before deletion.

        Raises:
            ValueError: If ``where`` resolves to no filter condition.
        """
        condition = apply_where(orm_class, where)
        if condition is None:
            msg = "where must contain at least one filter condition for delete_many"
            raise ValueError(msg)
        statement = delete(orm_class).where(condition).returning(orm_class).execution_options(synchronize_session=None)
        async with async_session() as session:
            await set_role(session, info.context.request.user)
            rows = (await session.execute(statement)).scalars().all()
            await session.commit()
        return list(rows)

    return {
        ResolverType.LIST: list_resolver,
        ResolverType.PK: pk_resolver,
        ResolverType.CREATE: create_resolver,
        ResolverType.CREATE_MANY: create_many_resolver,
        ResolverType.UPDATE: update_resolver,
        ResolverType.UPDATE_MANY: update_many_resolver,
        ResolverType.DELETE: delete_resolver,
        ResolverType.DELETE_MANY: delete_many_resolver,
    }[resolver_type]


class _UnmappableFunctionError(RuntimeError):
    """Raised when a :class:`FunctionInfo` can't be wired into the GraphQL schema.

    Used as a control-flow signal inside :func:`build`: the loop catches it,
    logs a warning, and skips the offending function rather than aborting the
    whole startup.
    """


def _function_return_annotation(fn: FunctionInfo, mapper: StrawberrySQLAlchemyMapper):
    """Resolve the GraphQL return-type annotation for a custom-query function.

    Args:
        fn: The function metadata.
        mapper: The active :class:`StrawberrySQLAlchemyMapper`. Required for
            ``ROW`` / ``SET`` returns so the previously-mapped table type can
            be looked up by its Pascal-cased singular name.

    Returns:
        A typing-compatible annotation:

        * ``T | None`` for ``SCALAR`` (where ``T`` is the mapped Python type),
        * ``GqlType | None`` for ``ROW``,
        * ``list[GqlType]`` for ``SET``.

    Raises:
        _UnmappableFunctionError: If the function's return table has no mapped
            Strawberry type yet.
    """
    if fn.return_kind == FunctionReturnKind.SCALAR:
        return fn.return_python_type | None
    type_name = to_pascal(inflect.singular_noun(fn.return_table_name) or fn.return_table_name)
    gql_type = mapper.mapped_types.get(type_name)
    if gql_type is None:
        raise _UnmappableFunctionError(
            f"return table {fn.return_table_name!r} has no GraphQL type mapped (expected {type_name!r})"
        )
    if fn.return_kind == FunctionReturnKind.SET:
        return list[gql_type]
    return gql_type | None


def _build_function_resolver(fn: FunctionInfo, base):
    """Build the async resolver that invokes a custom-query function.

    The resolver opens a fresh async session, applies the request user's
    PostgreSQL role, and issues a single ``SELECT`` against the function with
    PostgreSQL's named-argument call syntax (``arg := :arg``) so that any
    arguments left as :data:`strawberry.UNSET` fall back to the function's
    declared ``DEFAULT``.

    For ``ROW`` / ``SET`` returns the statement is wrapped in
    ``select(orm_class).from_statement(...)`` so the result rows come back as
    automap ORM instances — exactly the shape the
    ``strawberry-sqlalchemy-mapper``-generated field resolvers expect.
    ``SCALAR`` returns are read with ``.scalar()`` directly.

    Args:
        fn: The function metadata.
        base: The SQLAlchemy automap base; used to look up the ORM class for
            ``ROW`` / ``SET`` returns.

    Returns:
        An async callable suitable for ``strawberry.field(resolver=...)``.
        Its argument signature is patched after the fact via
        :func:`_set_resolver_arguments`.
    """
    qualified = f'"{fn.schema}"."{fn.name}"'

    async def resolver(info: strawberry.Info[CustomHTTPContextType, None], **kwargs: object):
        bind = {k: v for k, v in kwargs.items() if v is not strawberry.UNSET}
        placeholders = ", ".join(f"{name} := :{name}" for name in bind)
        async with async_session() as session:
            await set_role(session, info.context.request.user)
            if fn.return_kind == FunctionReturnKind.SCALAR:
                sql = f"SELECT {qualified}({placeholders}) AS value"
                return (await session.execute(text(sql).bindparams(**bind))).scalar()
            sql = f"SELECT * FROM {qualified}({placeholders})"
            orm_class = base.classes[fn.return_table_name]
            stmt = select(orm_class).from_statement(text(sql).bindparams(**bind))
            rows = (await session.execute(stmt)).scalars().all()
            if fn.return_kind == FunctionReturnKind.SET:
                return list(rows)
            return rows[0] if rows else None

    return resolver


def build(introspection: Introspection):
    """Build a Strawberry GraphQL schema and Litestar controller from an :class:`Introspection`.

    Each invocation produces an isolated ``StrawberrySQLAlchemyMapper`` plus
    fresh ``Query`` and ``Mutation`` skeletons, so the function is safe to
    call repeatedly (test reload, dev hot-reload, multiple Litestar apps in
    the same process).

    Per-table CRUD fields are derived from ``introspection.base.classes``,
    and one custom-query field per :class:`FunctionInfo` in
    ``introspection.functions`` is appended to the ``Query`` root after the
    table loop.

    Args:
        introspection: The introspection result produced by
            :func:`fusionserve.persistence.introspect`.

    Returns:
        A Litestar-compatible GraphQL controller ready to be mounted
        on the application.
    """
    mapper = StrawberrySQLAlchemyMapper(
        always_use_list=True,
        model_to_type_name=lambda model: to_pascal(inflect.singular_noun(model.__name__)),
    )
    _base = introspection.base

    class Query:
        """Root GraphQL query type for this build.

        Fields are populated below as the loop walks ``_base.classes``.
        """

        @strawberry.field
        def current_user(self, info: strawberry.Info[CustomHTTPContextType, None]) -> JWTUser | None:
            return info.context.request.user or None

    class Mutation:
        """Root GraphQL mutation type for this build."""

    for orm_class in _base.classes:
        table: Table = orm_class.__table__
        comment = SmartComment.from_object(table)
        pks = table.primary_key.columns.keys()
        gql_type = mapper.type(orm_class)(type(to_pascal(inflect.singular_noun(table.name)), (object,), {}))
        if comment.content:
            gql_type.__strawberry_definition__.description = comment.content
        _set_field_descriptions(gql_type, table)
        _rename_fk_fields(gql_type, table)
        # ---- Query: list ----
        setattr(
            Query,
            table.name,
            strawberry.field(
                resolver=resolver_factory(orm_class, gql_type, ResolverType.LIST, mapper),
                description=f"List {table.name} with pagination, filtering and ordering.",
            ),
        )
        list_field = getattr(Query, table.name)
        _set_resolver_arguments(
            list_field,
            [arg for arg in list_field.base_resolver.arguments if arg.python_name not in ("order_by", "where")]
            + [
                StrawberryArgument(
                    "order_by",
                    "order_by",
                    StrawberryAnnotation(create_order_by_input(orm_class.__table__) | None),  # type: ignore
                    default=strawberry.UNSET,
                ),
                StrawberryArgument(
                    "where",
                    "where",
                    StrawberryAnnotation(create_where_input(orm_class.__table__) | None),  # type: ignore
                    default=strawberry.UNSET,
                ),
            ],
        )
        # ---- Query: pk ----
        pk_field_name = inflect.singular_noun(table.name)
        setattr(
            Query,
            pk_field_name,
            strawberry.field(
                resolver=resolver_factory(orm_class, gql_type, ResolverType.PK, mapper),
                description=f"Get a {pk_field_name} by primary key.",
            ),
        )
        _set_resolver_arguments(
            getattr(Query, pk_field_name),
            [
                StrawberryArgument(
                    pk,
                    pk,
                    StrawberryAnnotation(mapper._convert_column_to_strawberry_type(table.primary_key.columns[pk])),
                )
                for pk in pks
            ],
        )
        # ---- Mutation: create one ----
        create_one = f"create{to_pascal(pk_field_name)}"
        setattr(
            Mutation,
            create_one,
            strawberry.mutation(
                resolver=resolver_factory(orm_class, gql_type, ResolverType.CREATE, mapper),
                description=f"Create a new {pk_field_name}.",
            ),
        )
        _set_resolver_arguments(
            getattr(Mutation, create_one),
            [
                StrawberryArgument(
                    "input",
                    "input",
                    StrawberryAnnotation(create_input_type(orm_class.__table__, mapper)),
                )
            ],
        )
        # ---- Mutation: create many ----
        create_many = f"create{to_pascal(table.name)}"
        setattr(
            Mutation,
            create_many,
            strawberry.mutation(
                resolver=resolver_factory(orm_class, gql_type, ResolverType.CREATE_MANY, mapper),
                description=f"Create many {table.name} rows in a single statement.",
            ),
        )
        _set_resolver_arguments(
            getattr(Mutation, create_many),
            [
                StrawberryArgument(
                    "inputs",
                    "inputs",
                    StrawberryAnnotation(list[create_input_type(orm_class.__table__, mapper)]),  # type: ignore
                ),
            ],
        )
        # ---- Mutation: update by pk ----
        update_one = f"update{to_pascal(pk_field_name)}"
        setattr(
            Mutation,
            update_one,
            strawberry.mutation(
                resolver=resolver_factory(orm_class, gql_type, ResolverType.UPDATE, mapper),
                description=f"Update an existing {pk_field_name} by primary key.",
            ),
        )
        _set_resolver_arguments(
            getattr(Mutation, update_one),
            [
                StrawberryArgument(
                    "patch",
                    "patch",
                    StrawberryAnnotation(patch_input_type(orm_class.__table__, mapper)),
                ),
                *[
                    StrawberryArgument(
                        pk,
                        pk,
                        StrawberryAnnotation(mapper._convert_column_to_strawberry_type(table.primary_key.columns[pk])),
                    )
                    for pk in pks
                ],
            ],
        )
        # ---- Mutation: update many ----
        update_many = f"update{to_pascal(table.name)}"
        setattr(
            Mutation,
            update_many,
            strawberry.mutation(
                resolver=resolver_factory(orm_class, gql_type, ResolverType.UPDATE_MANY, mapper),
                description=f"Update every {table.name} row matching the given where condition with the same patch.",
            ),
        )
        _set_resolver_arguments(
            getattr(Mutation, update_many),
            [
                StrawberryArgument(
                    "patch",
                    "patch",
                    StrawberryAnnotation(patch_input_type(orm_class.__table__, mapper)),
                ),
                StrawberryArgument(
                    "where",
                    "where",
                    StrawberryAnnotation(create_where_input(orm_class.__table__)),  # type: ignore
                ),
            ],
        )
        # ---- Mutation: delete by pk ----
        delete_one = f"delete{to_pascal(pk_field_name)}"
        setattr(
            Mutation,
            delete_one,
            strawberry.mutation(
                resolver=resolver_factory(orm_class, gql_type, ResolverType.DELETE, mapper),
                description=f"Delete an existing {pk_field_name} by primary key.",
            ),
        )
        _set_resolver_arguments(
            getattr(Mutation, delete_one),
            [
                StrawberryArgument(
                    pk,
                    pk,
                    StrawberryAnnotation(mapper._convert_column_to_strawberry_type(table.primary_key.columns[pk])),
                )
                for pk in pks
            ],
        )
        # ---- Mutation: delete many ----
        delete_many = f"delete{to_pascal(table.name)}"
        setattr(
            Mutation,
            delete_many,
            strawberry.mutation(
                resolver=resolver_factory(orm_class, gql_type, ResolverType.DELETE_MANY, mapper),
                description=f"Delete every {table.name} row matching the given where condition.",
            ),
        )
        _set_resolver_arguments(
            getattr(Mutation, delete_many),
            [
                StrawberryArgument(
                    "where",
                    "where",
                    StrawberryAnnotation(create_where_input(orm_class.__table__)),  # type: ignore
                ),
            ],
        )

    # ---- Custom queries from STABLE/IMMUTABLE PostgreSQL functions ----
    existing_query_fields = {name for name in dir(Query) if not name.startswith("__")}
    for fn in introspection.functions:
        field_name = to_camel_case(fn.name)
        if field_name in existing_query_fields:
            # TODO: collision-resolution strategy. Options under consideration:
            #   * prefix function fields with ``fn_`` or expose under a nested ``rpc`` namespace
            #   * allow function to win when explicitly opted-in via smart-comment metadata
            # For v1: existing query field wins, function is dropped.
            _logger.warning(
                "Skipping GraphQL exposure of function %s.%s: field name %r collides with an existing query field.",
                fn.schema,
                fn.name,
                field_name,
            )
            continue
        try:
            return_annotation = _function_return_annotation(fn, mapper)
        except _UnmappableFunctionError as exc:
            _logger.warning(
                "Skipping GraphQL exposure of function %s.%s: %s",
                fn.schema,
                fn.name,
                exc,
            )
            continue
        setattr(
            Query,
            field_name,
            strawberry.field(
                resolver=_build_function_resolver(fn, _base),
                description=fn.description or f"Custom query exposing {fn.schema}.{fn.name}().",
            ),
        )
        arguments: list[StrawberryArgument] = []
        for p in fn.params:
            annotation = StrawberryAnnotation(p.python_type | None if p.has_default else p.python_type)
            if p.has_default:
                arguments.append(StrawberryArgument(p.name, p.name, annotation, default=strawberry.UNSET))
            else:
                arguments.append(StrawberryArgument(p.name, p.name, annotation))
        _set_resolver_arguments(getattr(Query, field_name), arguments)
        # Override the inferred return type — the resolver itself is annotated
        # with ``Any`` because ``_build_function_resolver`` is not a closure
        # over the per-table types.
        getattr(Query, field_name).type_annotation = StrawberryAnnotation(return_annotation)
        existing_query_fields.add(field_name)

    # Models related to models in the schema are automatically mapped here.
    mapper.finalize()
    schema = strawberry.Schema(
        query=strawberry.type(Query),
        mutation=strawberry.type(Mutation),
        extensions=[
            QueryDepthLimiter(max_depth=10),
        ],
    )
    return make_graphql_controller(
        schema,
        path=f"{settings.base_path}/graphql",
        context_getter=custom_context_getter,
        allow_queries_via_get=False,
        graphql_ide="graphiql" if settings.ui_enabled else None,
        keep_alive=True,
    )
