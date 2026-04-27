"""GraphQL schema and resolver generation for FusionServe.

This module dynamically builds a Strawberry GraphQL schema from SQLAlchemy
automap models. For each registered table it creates two query fields: a
paginated list resolver and a primary-key lookup resolver. The schema is
then exposed via a Litestar-compatible GraphQL controller.
"""

import logging
from typing import Any, TypeVar

import inflect as _inflect
import strawberry
from litestar import Request, WebSocket
from litestar.datastructures import State
from pydantic.alias_generators import to_pascal
from sqlalchemy import Table, and_, delete, func, not_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.automap import AutomapBase
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
from strawberry.utils.str_converters import to_snake_case
from strawberry_sqlalchemy_mapper import StrawberrySQLAlchemyMapper

from .auth import User
from .config import settings
from .models import COMPARISON_TYPE_MAP, ResolverType, SortDirection
from .persistence import apply_load_only, async_session, set_role

_logger = logging.getLogger(settings.app_name)

inflect = _inflect.engine()
inflect.classical(names=0)

_mapper = StrawberrySQLAlchemyMapper()

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
    """Custom Strawberry context that carries an async SQLAlchemy session.

    Attributes:
        session: The active async SQLAlchemy session for the current request.
    """

    session: AsyncSession


class CustomHTTPContextType(HTTPContextType, CustomContext):
    """HTTP-specific context type combining Litestar HTTP context with custom context.

    Attributes:
        request: The typed Litestar HTTP request object.
    """

    request: Request[User, Any, State]


class CustomWSContextType(WebSocketContextType, CustomContext):
    """WebSocket-specific context type combining Litestar WS context with custom context.

    Attributes:
        socket: The typed Litestar WebSocket connection object.
    """

    socket: WebSocket[User, Any, State]


async def custom_context_getter(request: Request, session: AsyncSession) -> CustomContext:
    """Create a custom Strawberry context for each GraphQL request.

    Args:
        request: The incoming Litestar HTTP request.
        session: The async SQLAlchemy session provided by dependency injection.

    Returns:
        A ``CustomContext`` instance wrapping the session.
    """
    return CustomContext(session=session)


class Query:
    """Root GraphQL query type.

    Fields are dynamically added at startup by :func:`build` for each
    registered database table.
    """

    pass


class Mutation:
    """Root GraphQL mutation type.

    Fields are dynamically added at startup by :func:`build` for each
    registered database table.
    """

    pass


def columns_from_selections(
    selections: list[strawberry.types.nodes.Selection], orm_class: DeclarativeMeta
) -> list[str]:
    """Extract column names from a Strawberry GraphQL selection set.

    Recursively traverses the selection AST and collects snake_case
    column names that exist on the given ORM table. Handles both
    ``SelectedField`` and ``FragmentSpread`` node types.

    Args:
        selections: The list of Strawberry selection nodes to inspect.
        table: The SQLAlchemy ORM class whose ``__table__.columns``
            are used to validate column existence.

    Returns:
        A list of column name strings in snake_case that match actual
        table columns.
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
        defaults[column.name] = strawberry.UNSET

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
        defaults[column.name] = strawberry.UNSET

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


def create_input_type(table: Table) -> type:
    """Dynamically create a Strawberry input type for creating a new record.

    Generates a class named ``{PascalTableName}Input`` with one optional
    field per column. Unset fields default to ``strawberry.UNSET`` so that
    omitted columns can be handled with database defaults or nullable
    constraints.

    Args:
        table: The SQLAlchemy ``Table`` whose columns drive the input fields.
        gql_type: The corresponding Strawberry GraphQL type for the table.

    Returns:
        A Strawberry ``@input``-decorated class.
    """
    annotations: dict[str, type] = {}
    defaults: dict[str, object] = {}
    for column in table.columns:
        try:
            column_type = _mapper._convert_column_to_strawberry_type(column)
        except NotImplementedError:
            column_type = str
        annotations[column.name] = column_type
        defaults[column.name] = strawberry.UNSET
        if column.server_default is not None:
            annotations[column.name] = column_type | None

    class_name = f"{to_pascal(table.name)}Input"
    cls = type(class_name, (object,), {"__annotations__": annotations, **defaults})
    return strawberry.input(cls)


def patch_input_type(table: Table) -> type:
    """Dynamically create a Strawberry input type for patching an existing record.

    Generates a class named ``{PascalTableName}Patch`` with one optional
    field per non-primary-key column. All fields default to
    ``strawberry.UNSET`` so that omitted columns are left untouched during
    the update. Primary key columns are excluded because they identify the
    target record and are passed as separate resolver arguments.

    Args:
        table: The SQLAlchemy ``Table`` whose columns drive the input fields.

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
            column_type = _mapper._convert_column_to_strawberry_type(column)
        except NotImplementedError:
            column_type = str
        annotations[column.name] = column_type | None
        defaults[column.name] = strawberry.UNSET

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
):
    """Create a Strawberry resolver function for a given table and resolver type.

    Returns either a paginated list resolver or a primary-key lookup resolver
    depending on ``resolver_type``. Both resolvers apply column-level load
    optimisation based on the GraphQL selection set and execute queries in
    an independent async session with the configured anonymous role.

    Args:
        orm_class: The SQLAlchemy ORM class representing the underlying table.
        gql_type: The Strawberry GraphQL type mapped from the ORM class.
        resolver_type: Whether to create a ``LIST`` or ``PK`` resolver.
        order_by_type: The dynamically generated Strawberry input type for
            ordering (only used by ``LIST`` resolvers).
        where_type: The dynamically generated Strawberry input type for
            filtering (only used by ``LIST`` resolvers).
    Returns:
        An async resolver function suitable for
        ``strawberry.field(resolver=...)``.
    """

    async def list_resolver(
        info: strawberry.Info[CustomHTTPContextType, None],
        limit: int = settings.max_page_length,
        offset: int = 0,
        order_by: create_order_by_input(orm_class.__table__) | None = None,  # type: ignore
        where: create_where_input(orm_class.__table__) | None = None,  # type: ignore
    ) -> PaginationWindow[gql_type]:  # type: ignore
        """Resolve a paginated list of records for the table.

        Args:
            info: The Strawberry resolver info containing the selection set
                and custom context.
            limit: Maximum number of records to return.
            offset: Number of records to skip before returning results.
            order_by: Optional per-column ordering input (dynamically typed
                per table).
            where: Optional per-column filter input with boolean combinators
                (dynamically typed per table).

        Returns:
            A ``PaginationWindow`` containing the matching nodes and
            total count.
        """
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
        else:
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
            Exception: If no record is found for the given primary key(s).
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
            result = (await session.execute(statement)).scalar_one()
        if result:
            return result
        else:
            raise Exception("not found")

    async def create_resolver(
        info: strawberry.Info[CustomHTTPContextType, None],
        input: create_input_type(orm_class.__table__),  # type: ignore
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

    async def update_resolver(
        info: strawberry.Info[CustomHTTPContextType, None],
        patch: patch_input_type(orm_class.__table__),  # type: ignore
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
            Exception: If no record is found for the given primary key(s).
        """
        statement = select(orm_class)
        for key, id in kwids.items():
            statement = statement.where(getattr(orm_class, key) == id)
        async with async_session() as session:
            await set_role(session, info.context.request.user)
            result = (await session.execute(statement)).scalar_one_or_none()
            if result is None:
                raise Exception("not found")
            for key, value in vars(patch).items():
                if value is strawberry.UNSET:
                    continue
                setattr(result, key, value)
            await session.commit()
            await session.refresh(result)
        return result

    async def update_many_resolver(
        info: strawberry.Info[CustomHTTPContextType, None],
        patch: patch_input_type(orm_class.__table__),  # type: ignore
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
            Exception: If no record is found for the given primary key(s).
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
                raise Exception("not found")
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
        ResolverType.UPDATE: update_resolver,
        ResolverType.UPDATE_MANY: update_many_resolver,
        ResolverType.DELETE: delete_resolver,
        ResolverType.DELETE_MANY: delete_many_resolver,
    }[resolver_type]


def build(_base: AutomapBase):
    """Build a Strawberry GraphQL schema and Litestar controller from reflected tables.

    Iterates over the model registry, creates Strawberry types via
    ``StrawberrySQLAlchemyMapper``, and registers a paginated list query
    field and a primary-key lookup query field for each table on the
    root ``Query`` type.

    Args:
        _base: The SQLAlchemy automap base whose ``.classes`` attribute
            maps table names to ORM classes.

    Returns:
        A Litestar-compatible GraphQL controller ready to be mounted
        on the application.
    """

    for orm_class in _base.classes:
        table: Table = orm_class.__table__
        pks = table.primary_key.columns.keys()
        orm_class: DeclarativeMeta = _base.classes.get(table.name)
        gql_type = _mapper.type(orm_class)(type(table.name, (object,), {}))
        setattr(
            Query,
            table.name,
            strawberry.field(
                resolver=resolver_factory(
                    orm_class,
                    gql_type,
                    ResolverType.LIST,
                ),
                description=f"List {table.name} with pagination, filtering and ordering.",
            ),
        )
        # dynamic order_by and where arguments for list resolver
        list_field = getattr(Query, table.name)
        list_field.base_resolver.arguments = [
            arg for arg in list_field.base_resolver.arguments if arg.python_name not in ("order_by", "where")
        ] + [
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
        ]
        setattr(
            Query,
            inflect.singular_noun(table.name),
            strawberry.field(
                resolver=resolver_factory(orm_class, gql_type, ResolverType.PK),
                description=f"Get a {inflect.singular_noun(table.name)} by primary key.",
            ),
        )
        # dynamic pk arguments
        getattr(Query, inflect.singular_noun(table.name)).base_resolver.arguments = [
            StrawberryArgument(
                pk, pk, StrawberryAnnotation(_mapper._convert_column_to_strawberry_type(table.primary_key.columns[pk]))
            )
            for pk in pks
        ]
        setattr(
            Mutation,
            f"create{to_pascal(inflect.singular_noun(table.name))}",
            strawberry.mutation(
                resolver=resolver_factory(orm_class, gql_type, ResolverType.CREATE),
                description=f"Create a new {inflect.singular_noun(table.name)}.",
            ),
        )
        # dynamic arguments
        """getattr(Mutation, inflect.singular_noun(table.name)).base_resolver.arguments = [
            StrawberryArgument(
                col, col, StrawberryAnnotation(mapper._convert_column_to_strawberry_type(table.columns[col]))
            )
            for col in cols
        ]"""
        getattr(Mutation, f"create{to_pascal(inflect.singular_noun(table.name))}").base_resolver.arguments = [
            StrawberryArgument("input", "input", StrawberryAnnotation(create_input_type(orm_class.__table__)))
        ]
        setattr(
            Mutation,
            f"update{to_pascal(inflect.singular_noun(table.name))}",
            strawberry.mutation(
                resolver=resolver_factory(orm_class, gql_type, ResolverType.UPDATE),
                description=f"Update an existing {inflect.singular_noun(table.name)} by primary key.",
            ),
        )
        # dynamic patch + pk arguments for update mutation
        getattr(Mutation, f"update{to_pascal(inflect.singular_noun(table.name))}").base_resolver.arguments = [
            StrawberryArgument(
                "patch",
                "patch",
                StrawberryAnnotation(patch_input_type(orm_class.__table__)),
            ),
            *[
                StrawberryArgument(
                    pk,
                    pk,
                    StrawberryAnnotation(_mapper._convert_column_to_strawberry_type(table.primary_key.columns[pk])),
                )
                for pk in pks
            ],
        ]
        setattr(
            Mutation,
            f"update{to_pascal(table.name)}",
            strawberry.mutation(
                resolver=resolver_factory(
                    orm_class,
                    gql_type,
                    ResolverType.UPDATE_MANY,
                ),
                description=f"Update every {table.name} row matching the given where condition with the same patch.",
            ),
        )
        # dynamic patch + where arguments for update_many mutation
        getattr(Mutation, f"update{to_pascal(table.name)}").base_resolver.arguments = [
            StrawberryArgument(
                "patch",
                "patch",
                StrawberryAnnotation(patch_input_type(orm_class.__table__)),
            ),
            StrawberryArgument(
                "where",
                "where",
                StrawberryAnnotation(create_where_input(orm_class.__table__)),  # type: ignore
            ),
        ]
        setattr(
            Mutation,
            f"delete{to_pascal(inflect.singular_noun(table.name))}",
            strawberry.mutation(
                resolver=resolver_factory(orm_class, gql_type, ResolverType.DELETE),
                description=f"Delete an existing {inflect.singular_noun(table.name)} by primary key.",
            ),
        )
        # dynamic pk arguments for delete mutation
        getattr(Mutation, f"delete{to_pascal(inflect.singular_noun(table.name))}").base_resolver.arguments = [
            StrawberryArgument(
                pk,
                pk,
                StrawberryAnnotation(_mapper._convert_column_to_strawberry_type(table.primary_key.columns[pk])),
            )
            for pk in pks
        ]
        setattr(
            Mutation,
            f"delete{to_pascal(table.name)}",
            strawberry.mutation(
                resolver=resolver_factory(
                    orm_class,
                    gql_type,
                    ResolverType.DELETE_MANY,
                ),
                description=f"Delete every {table.name} row matching the given where condition.",
            ),
        )
        # dynamic where argument for delete_many mutation
        getattr(Mutation, f"delete{to_pascal(table.name)}").base_resolver.arguments = [
            StrawberryArgument(
                "where",
                "where",
                StrawberryAnnotation(create_where_input(orm_class.__table__)),  # type: ignore
            ),
        ]

    # models that are related to models that are in the schema
    # are automatically mapped at this stage
    _mapper.finalize()
    schema = strawberry.Schema(
        query=strawberry.type(Query),
        mutation=strawberry.type(Mutation),
        extensions=[
            QueryDepthLimiter(max_depth=10),
        ],
        #  only needed if you have polymorphic types
        # types=mapper.mapped_types.values(),
    )
    controller = make_graphql_controller(
        schema,
        path=f"{settings.base_path}/graphql",
        context_getter=custom_context_getter,
        allow_queries_via_get=False,
        graphql_ide="graphiql",
        keep_alive=True,
    )
    return controller
