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
from sqlalchemy import Table, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.automap import AutomapBase
from sqlalchemy.orm import DeclarativeMeta
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

from .config import settings
from .models import PaginationParams, RegistryItem, ResolverType, SortDirection
from .persistence import apply_load_only, async_session, set_role

_logger = logging.getLogger(settings.app_name)

inflect = _inflect.engine()
inflect.classical(names=0)

Item = TypeVar("Item")


# TODO: auth
class User:
    """Placeholder user class for authentication context.

    This will be replaced with a proper user model once the
    authentication system is implemented.
    """

    pass


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


def columns_from_selections(selections: list[strawberry.types.nodes.Selection], table: DeclarativeMeta) -> list[str]:
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
            and to_snake_case(selection.name) in table.__table__.columns
        ):
            selected_columns.append(to_snake_case(selection.name))
        if isinstance(selection, strawberry.types.nodes.FragmentSpread):
            cols = [to_snake_case(col.name) for col in selection.selections]
            selected_columns.extend(cols)
        if len(selection.selections) > 0:
            # recursively get nested selections
            selected_columns.extend(columns_from_selections(selection.selections, table))
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


def create_resolver(table_name: str, gql_type, resolver_type: ResolverType, order_by_type: type | None = None):
    """Create a Strawberry resolver function for a given table and resolver type.

    Returns either a paginated list resolver or a primary-key lookup resolver
    depending on ``resolver_type``. Both resolvers apply column-level load
    optimisation based on the GraphQL selection set and execute queries in
    an independent async session with the configured anonymous role.

    Args:
        table_name: The database table name to resolve against.
        gql_type: The Strawberry GraphQL type mapped from the ORM class.
        resolver_type: Whether to create a ``LIST`` or ``PK`` resolver.
        order_by_type: The dynamically generated Strawberry input type for
            ordering (only used by ``LIST`` resolvers).

    Returns:
        An async resolver function suitable for
        ``strawberry.field(resolver=...)``.
    """
    orm_class: DeclarativeMeta = Base.classes.get(table_name)

    # TODO: implement filtering
    async def list_resolver(
        info: strawberry.Info[CustomHTTPContextType, None],
        limit: int = settings.max_page_length,
        offset: int = 0,
        order_by: order_by_type | None = None,  # type: ignore
        # advanced_filter: AdvancedFilter = None,
    ) -> PaginationWindow[gql_type]:  # type: ignore
        """Resolve a paginated list of records for the table.

        Args:
            info: The Strawberry resolver info containing the selection set
                and custom context.
            limit: Maximum number of records to return.
            offset: Number of records to skip before returning results.
            order_by: Optional per-column ordering input (dynamically typed
                per table).

        Returns:
            A ``PaginationWindow`` containing the matching nodes and
            total count.
        """
        statement = select(orm_class, func.count().over().label("total_count")).limit(limit).offset(offset)
        if order_by is not None:
            statement = apply_order_by(statement, orm_class, order_by)
        # the resolver is called for each field, so `selected_fields[0]` is always set
        selected_columns = columns_from_selections(info.selected_fields[0].selections, orm_class)
        statement = apply_load_only(statement, orm_class, selected_columns)
        # if the users requests more than one top level field we run concurrently in a thread.
        # the context session is managed at the request level so we need a new one here
        async with async_session() as session:
            await set_role(session)
            rows = (await session.execute(statement)).all()
        if rows:
            return PaginationWindow[gql_type](
                nodes=[row[0] for row in rows if row[0] is not None],
                total_count=rows[0][1],
            )
        else:
            return PaginationWindow[gql_type](nodes=[], total_count=0)

    # TODO: make primary keys dinamic instead of assuming it's always "id"
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
            await set_role(session)
            result = (await session.execute(statement)).scalar_one()
        if result:
            return result
        else:
            raise Exception("not found")

    return {ResolverType.LIST: list_resolver, ResolverType.PK: pk_resolver}[resolver_type]


def build(_base: AutomapBase, _registry: dict[str, RegistryItem]):
    """Build a Strawberry GraphQL schema and Litestar controller from reflected tables.

    Iterates over the model registry, creates Strawberry types via
    ``StrawberrySQLAlchemyMapper``, and registers a paginated list query
    field and a primary-key lookup query field for each table on the
    root ``Query`` type.

    Args:
        _base: The SQLAlchemy automap base whose ``.classes`` attribute
            maps table names to ORM classes.
        _registry: A mapping of table name to :class:`~fusionserve.models.RegistryItem`
            containing the Pydantic models for each table.

    Returns:
        A Litestar-compatible GraphQL controller ready to be mounted
        on the application.
    """
    global Base, models_registry
    Base = _base
    models_registry = _registry
    mapper = StrawberrySQLAlchemyMapper()
    for key, _item in _registry.items():
        table: Table = _base.classes.get(key).__table__
        pks = table.primary_key.columns.keys()
        strawberry.input(PaginationParams)
        orm_class: DeclarativeMeta = Base.classes.get(table.name)
        gql_type = mapper.type(orm_class)(type(table.name, (object,), {}))
        order_by_type = create_order_by_input(table)
        setattr(
            Query,
            table.name,
            strawberry.field(
                resolver=create_resolver(table.name, gql_type, ResolverType.LIST, order_by_type=order_by_type),
                description=f"List {table.name} with pagination, filtering and ordering.",
            ),
        )
        # dynamic order_by argument for list resolver
        list_field = getattr(Query, table.name)
        list_field.base_resolver.arguments = [
            arg for arg in list_field.base_resolver.arguments if arg.python_name != "order_by"
        ] + [
            StrawberryArgument(
                "order_by",
                "order_by",
                StrawberryAnnotation(order_by_type | None),  # type: ignore
                default=strawberry.UNSET,
            ),
        ]
        setattr(
            Query,
            inflect.singular_noun(table.name),
            strawberry.field(
                resolver=create_resolver(table.name, gql_type, ResolverType.PK),
                description=f"Get a {inflect.singular_noun(table.name)} by primary key.",
            ),
        )
        # dynamic pk arguments
        getattr(Query, inflect.singular_noun(table.name)).base_resolver.arguments = [
            StrawberryArgument(
                pk, pk, StrawberryAnnotation(mapper._convert_column_to_strawberry_type(table.primary_key.columns[pk]))
            )
            for pk in pks
        ]
    # models that are related to models that are in the schema
    # are automatically mapped at this stage
    mapper.finalize()
    schema = strawberry.Schema(
        strawberry.type(Query),
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
