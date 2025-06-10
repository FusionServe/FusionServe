import logging
from typing import Annotated, Any, Dict, Generic, List, Optional, Type, TypeVar

import strawberry
from fastapi import APIRouter, Depends, FastAPI
from icecream import ic
from sqlalchemy import Table, func, select
from sqlalchemy.ext.automap import AutomapBase
from sqlalchemy.orm import Bundle, DeclarativeMeta, load_only
from strawberry.extensions import QueryDepthLimiter
from strawberry.fastapi import GraphQLRouter
from strawberry.schema.config import StrawberryConfig
from strawberry_sqlalchemy_mapper import (
    StrawberrySQLAlchemyLoader,
    StrawberrySQLAlchemyMapper,
)

from .config import settings
from .models import AdvancedFilter, PaginationParams, RegistryItem
from .persistence import get_async_session, introspect, set_role

_logger = logging.getLogger(settings.app_name)

Item = TypeVar("Item")


@strawberry.type
class PaginationWindow(Generic[Item]):
    nodes: List[Item] = strawberry.field(
        description="The list of items in this pagination window."
    )
    total_count: int = strawberry.field(
        description="Total number of items in the filtered dataset."
    )


@strawberry.type
class Book:
    title: str
    author: str


def get_books(
    info: strawberry.Info,
) -> List[Book]:
    return [
        Book(
            title="The Great Gatsby",
            author="F. Scott Fitzgerald",
        ),
    ]


class Query:
    pass


def create_resolver(table_name: str, gql_type):

    async def resolver(
        info: strawberry.Info,
        limit: int = settings.max_page_lenght,
        offset: int = 0,
        order_by: Optional[str] = None,
        # advanced_filter: AdvancedFilter = None,
    ) -> PaginationWindow[gql_type]:  # type: ignore
        statement = (
            select(
                Base.classes.get(table_name), func.count().over().label("total_count")
            )
            .options(load_only(*get_selected_fields(info, gql_type)))
            .limit(limit)
            .offset(offset)
        )
        await set_role(info.context["session"])
        rows = (await info.context["session"].execute(statement)).all()
        print(gql_type.is_type_of)
        return PaginationWindow[gql_type](
            nodes=[row[0] for row in rows if row[0] is not None],
            total_count=rows[0][1],
        )

    return resolver


# context is expected to have an instance of StrawberrySQLAlchemyLoader
async def get_context(
    session=Depends(get_async_session),
):
    return {
        "session": session,
        "sqlalchemy_loader": StrawberrySQLAlchemyLoader(bind=session),
    }


# a custom resoolver is needed if resolvers return dict, trying to avoid
def custom_resolver(obj, field):
    print(f"Custom resolver called, obj: {obj}, field: {field}")
    try:
        return obj[field]
    except (KeyError, TypeError):
        return getattr(obj, field)


def build(_base: AutomapBase, _registry: dict[str, RegistryItem]):
    global Base, models_registry
    Base = _base
    models_registry = _registry
    mapper = StrawberrySQLAlchemyMapper()
    for key, item in _registry.items():
        table: Table = _base.classes.get(key).__table__
        pks = table.primary_key.columns.keys()
        print(table.name)
        strawberry.input(PaginationParams)
        orm_class: DeclarativeMeta = Base.classes.get(table.name)
        gql_type = mapper.type(orm_class)(type(table.name, (object,), {}))
        setattr(
            Query,
            table.name,
            strawberry.field(resolver=create_resolver(table.name, gql_type)),
        )
    setattr(Query, "books", strawberry.field(resolver=get_books))
    # models that are related to models that are in the schema
    # are automatically mapped at this stage
    mapper.finalize()
    # only needed if you have polymorphic types
    additional_types = list(mapper.mapped_types.values())
    schema = strawberry.Schema(
        strawberry.type(Query),
        extensions=[
            QueryDepthLimiter(max_depth=10),
        ],
        types=additional_types,
    )
    return GraphQLRouter(
        schema,
        allow_queries_via_get=False,
        keep_alive=True,
        debug=True,
        prefix="/graphql",
        context_getter=get_context,
    )


def get_selected_fields(info: strawberry.Info, gql_type):
    """
    Extracts the fields requested in the GraphQL query for a specific type.

    Args:
        info: The strawberry.Info object.
        gql_type: The GraphQL type.

    Returns:
        A list of sqlalchemy orm attributes suitable to be used in a load_only() statement.
    """
    nodes = [x for x in info.selected_fields[0].selections if x.name == "nodes"]
    return [
        getattr(Base.classes.get(gql_type.__name__), x.name)
        for x in nodes[0].selections
    ]
