import logging
from typing import Any, TypeVar

import inflect as _inflect
import strawberry
from litestar import Request, WebSocket
from litestar.datastructures import State
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
from .models import PaginationParams, RegistryItem, ResolverType
from .persistence import apply_load_only, async_session, set_role

_logger = logging.getLogger(settings.app_name)

inflect = _inflect.engine()
inflect.classical(names=0)

Item = TypeVar("Item")


# TODO: auth
class User:
    pass


@strawberry.type
class PaginationWindow[Item]:
    nodes: list[Item] = strawberry.field(description="The list of items in this pagination window.")
    total_count: int = strawberry.field(description="Total number of items in the filtered dataset.")


class CustomContext(BaseContext, kw_only=True):
    session: AsyncSession


class CustomHTTPContextType(HTTPContextType, CustomContext):
    request: Request[User, Any, State]


class CustomWSContextType(WebSocketContextType, CustomContext):
    socket: WebSocket[User, Any, State]


async def custom_context_getter(request: Request, session: AsyncSession) -> CustomContext:
    return CustomContext(session=session)


class Query:
    pass


def columns_from_selections(selections: list[strawberry.types.nodes.Selection], table: DeclarativeMeta) -> list[str]:
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


def create_resolver(table_name: str, gql_type, resolver_type: ResolverType):
    orm_class: DeclarativeMeta = Base.classes.get(table_name)

    # TODO: implement filtering and ordering
    async def list_resolver(
        info: strawberry.Info[CustomHTTPContextType, None],
        limit: int = settings.max_page_length,
        offset: int = 0,
        order_by: str | None = None,
        # advanced_filter: AdvancedFilter = None,
    ) -> PaginationWindow[gql_type]:  # type: ignore
        statement = select(orm_class, func.count().over().label("total_count")).limit(limit).offset(offset)
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
        setattr(
            Query,
            table.name,
            strawberry.field(
                resolver=create_resolver(table.name, gql_type, ResolverType.LIST),
                description=f"List {table.name} with pagination, filtering and ordering.",
            ),
        )
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
