import logging
from typing import Any, TypeVar

import strawberry
from litestar import Request, WebSocket
from litestar.datastructures import State
from pydantic.alias_generators import to_snake
from sqlalchemy import Table, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.automap import AutomapBase
from sqlalchemy.orm import DeclarativeMeta
from strawberry.extensions import QueryDepthLimiter
from strawberry.litestar import (
    BaseContext,
    HTTPContextType,
    WebSocketContextType,
    make_graphql_controller,
)
from strawberry_sqlalchemy_mapper import StrawberrySQLAlchemyMapper

from .config import settings
from .models import PaginationParams, RegistryItem
from .persistence import apply_load_only, async_session, set_role

_logger = logging.getLogger(settings.app_name)

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


def columns_from_selections(selections: list[strawberry.types.nodes.Selection]) -> list[str] | None:
    selected_columns: list[str] | None = None
    for selection in selections:
        if selection.name == "nodes":
            selected_columns = []
            for x in selection.selections:
                if isinstance(x, strawberry.types.nodes.SelectedField):
                    print(to_snake(x.name))
                    selected_columns.append(to_snake(x.name))
                if isinstance(x, strawberry.types.nodes.FragmentSpread):
                    cols = [to_snake(col.name) for col in x.selections]
                    selected_columns.extend(cols)
    return selected_columns


def create_resolver(table_name: str, gql_type):

    async def resolver(
        info: strawberry.Info[CustomHTTPContextType, None],
        limit: int = settings.max_page_length,
        offset: int = 0,
        order_by: str | None = None,
        # advanced_filter: AdvancedFilter = None,
    ) -> PaginationWindow[gql_type]:  # type: ignore
        statement = (
            select(Base.classes.get(table_name), func.count().over().label("total_count")).limit(limit).offset(offset)
        )
        # the resolver is called for each field, so `selected_fields[0]` is always set
        selected_columns = columns_from_selections(info.selected_fields[0].selections)
        statement = apply_load_only(statement, Base.classes.get(table_name), selected_columns)
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

    return resolver


def build(_base: AutomapBase, _registry: dict[str, RegistryItem]):
    global Base, models_registry
    Base = _base
    models_registry = _registry
    mapper = StrawberrySQLAlchemyMapper()
    for key, _item in _registry.items():
        table: Table = _base.classes.get(key).__table__
        _pks = table.primary_key.columns.keys()
        strawberry.input(PaginationParams)
        orm_class: DeclarativeMeta = Base.classes.get(table.name)
        gql_type = mapper.type(orm_class)(type(table.name, (object,), {}))
        setattr(
            Query,
            table.name,
            strawberry.field(resolver=create_resolver(table.name, gql_type)),
        )
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
