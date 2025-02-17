import asyncio
import re
import uuid
from datetime import datetime, timedelta
from enum import Enum
from typing import Annotated, Any, ClassVar, Dict, List, Literal, Set, Tuple
from zoneinfo import ZoneInfo

import inflect as _inflect
import odata_query
import odata_query.exceptions
import odata_query.sqlalchemy
from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse
from icecream import ic
from pydantic import BaseModel, ConfigDict, Field, create_model
from pydantic.alias_generators import to_camel, to_pascal
from sqlalchemy import (
    Column,
    MetaData,
    Table,
    create_engine,
    func,
    insert,
    inspect,
    select,
    text,
    update,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.automap import AutomapBase, automap_base
from sqlalchemy.orm import DeclarativeBase, DeclarativeMeta

from .config import logger as _logger
from .config import settings

engine = create_async_engine(
    f"postgresql+asyncpg://{settings.pg_user}:{settings.pg_password}@"
    f"{settings.pg_host}:"
    f"{'5432'}/{settings.pg_database}",
    echo=settings.echo_sql,
    pool_pre_ping=True,
)


async def get_async_session():
    async_session = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    async with async_session() as session:
        yield session


class RegistryItem(BaseModel):
    model: Any = None
    get_input: Any = None
    create_input: Any = None
    pk_input: Any = None


class PaginationParams(BaseModel):
    limit: int = Field(100, alias="_limit", gt=0, le=settings.max_page_lenght)
    offset: int = Field(0, alias="_offset", ge=0)
    order_by: str | None = Field(None, alias="_order_by")


# TODO: review validation pattern
pattern = r"^\(?\s*([a-zA-Z_]+)\s+(eq|ne|gt|ge|lt|le)\s+('[^']*'|\d+(\.\d+)?)\s*(\s+(and|or)\s+"
pattern += r"""\(?\s*([a-zA-Z_]+)\s+(eq|ne|gt|ge|lt|le)\s+('[^']*'|\d+(\.\d+)?)\s*\)?\s*)*$"""


class AdvancedFilter(BaseModel):
    filter: str | None = Field(
        None,
        alias="_filter",
        description="advanced **filter** on multiple fields using expressions",
        examples="(author eq 'Kafka' or name eq 'Mike') and price lt 2.55",
        pattern=pattern,
    )


models_registry: Dict[str, RegistryItem] = {}
Base: AutomapBase = None
inflect = _inflect.engine()
inflect.classical(names=0)


def pydantic_field_from_column(
    column: Column, model_type: Literal["model", "get_input", "create_input", "pk_input"]
) -> Tuple[Any, Field]:
    try:
        python_type = column.type.python_type
    except NotImplementedError:
        python_type = str
    field_type = {
        "model": python_type | None if column.nullable else python_type,
        "get_input": python_type | None,
        "create_input": python_type | None if not column.primary_key else python_type,
        "pk_input": python_type if column.primary_key else None,
    }[model_type]
    return (field_type, Field(None, description=column.comment))


def introspect():
    # Introspection is only supported for sync engines
    _engine = create_engine(
        f"postgresql+psycopg://{settings.pg_user}:{settings.pg_password}@"
        f"{settings.pg_host}:"
        f"{'5432'}/{settings.pg_database}",
        echo=settings.echo_sql,
        pool_pre_ping=True,
    )
    metadata = MetaData()
    metadata.reflect(bind=_engine, schema=settings.pg_app_schema)
    global Base
    Base = automap_base(metadata=metadata)
    # calling prepare() just sets up mapped classes and relationships.
    Base.prepare()
    for table in metadata.sorted_tables:
        if not inflect.singular_noun(table.name):
            raise ValueError(f"Table name {table.name} is not plural")
        item = RegistryItem()
        for model_type in RegistryItem.model_fields.keys():
            setattr(
                item,
                model_type,
                create_model(
                    to_pascal(f"{inflect.singular_noun(table.name)}_{model_type}"),
                    __config__=ConfigDict(from_attributes=True),
                    **{
                        k: pydantic_field_from_column(v, model_type)
                        for k, v in table.columns.items()
                        if pydantic_field_from_column(v, model_type)[0]
                    },
                ),
            )
        models_registry[table.name] = item


async def set_role(session: AsyncSession):
    # TODO: role from jwt or anonymous
    role = settings.anonymous_role
    await session.execute(text(f"SET ROLE '{role}'"))


def create_endpoint(table_name: str, endpoint_type: str):
    endpoint = {}
    orm_class: DeclarativeMeta = Base.classes.get(table_name)
    if endpoint_type == "list":
        get_input = models_registry[table_name].get_input
        async def endpoint(
            # request: Request,
            basic_filter: Annotated[get_input, Query(), Depends()],  # type: ignore
            pagination: Annotated[PaginationParams, Query(), Depends()] = None,
            advanced_filter: Annotated[AdvancedFilter, Query(), Depends()] = None,
            session: AsyncSession = Depends(get_async_session),
        ):
            await set_role(session)
            statement = (
                select(orm_class).limit(pagination.limit).offset(pagination.offset)
            )
            for k in basic_filter.model_fields:
                # skip attributes not in query string
                if getattr(basic_filter, k):
                    # add the where condition to select expression
                    statement = statement.where(
                        getattr(orm_class, k) == getattr(basic_filter, k)
                    )
            try:
                print(advanced_filter)
                if advanced_filter.filter:
                    statement = odata_query.sqlalchemy.apply_odata_query(
                        statement, advanced_filter.filter
                    )
            except (
                odata_query.exceptions.InvalidFieldException,
                odata_query.exceptions.ParsingException,
            ) as e:
                # TODO: standardize error responses as the best practises
                return JSONResponse({"error": str(e)})
            results = (await session.execute(statement)).scalars().all()
            return results

    if endpoint_type == "get_one":
        pk_input = models_registry[table_name].pk_input
        async def endpoint(
            request: Request,
            pk: Annotated[pk_input, Depends()],  # type: ignore
            session: AsyncSession = Depends(get_async_session),
        ):
            await set_role(session)
            return await session.get(orm_class, pk.model_dump())

    if endpoint_type == "create":
        # TODO: create a specific input model with required fields(Not nulls w/o defaults)
        create_input = models_registry[table_name].get_input
        async def endpoint(
            # request: Request,
            input: List[Annotated[create_input, Depends()]],  # type: ignore
            session: AsyncSession = Depends(get_async_session),
        ):
            await set_role(session)
            try:
                results = (await session.execute(insert(orm_class).returning(orm_class),
                            input,
                )).scalars().all()
            except IntegrityError as e:
                # TODO: standardize error responses as the best practises
                lines = str(e.orig).splitlines()
                return JSONResponse({"error": lines[0].split(":")[1].strip(), "detail": lines[1].split(":")[1].strip()})
            await session.commit()
            return results
    
    # TODO: is replace really needed?

    if endpoint_type == "update":
        pk_input = models_registry[table_name].pk_input
        # TODO: create a specific input model with required fields(Not nulls w/o defaults)
        update_input = models_registry[table_name].get_input
        async def endpoint(
            # request: Request,
            pk: Annotated[pk_input, Depends()],  # type: ignore
            input: Annotated[update_input, Depends()],  # type: ignore
            session: AsyncSession = Depends(get_async_session),
        ):
            await set_role(session)
            try:
                item = await session.get(orm_class, pk.model_dump())
                for k, v in input.model_dump(exclude_unset=True, exclude_none = True).items():
                    setattr(item, k, v)
                session.add(item)
            except IntegrityError as e:
                # TODO: standardize error responses as the best practises
                lines = str(e.orig).splitlines()
                return JSONResponse({"error": lines[0].split(":")[1].strip(), "detail": lines[1].split(":")[1].strip()})
            await session.commit()
            return item
    
    if endpoint_type == "delete":
        pk_input = models_registry[table_name].pk_input
        async def endpoint(
            # request: Request,
            pk: Annotated[pk_input, Depends()],  # type: ignore
            session: AsyncSession = Depends(get_async_session),
        ):
            await set_role(session)
            try:
                item = await session.get(orm_class, pk.model_dump())
                await session.delete(item)
            except IntegrityError as e:
                # TODO: deal with fk constraints
                # TODO: standardize error responses as the best practises
                lines = str(e.orig).splitlines()
                return JSONResponse({"error": lines[0].split(":")[1].strip(), "detail": lines[1].split(":")[1].strip()})
            await session.commit()
            return item
        
    return endpoint


def add_routes(app: FastAPI):
    introspect()
    for key, item in models_registry.items():
        table: Table = Base.classes.get(key).__table__
        pks = table.primary_key.columns.keys()
        "/".join([f"{{{pk}}}" for pk in pks])
        # list
        app.add_api_route(
            f"/api/{key.lower()}",
            create_endpoint(key, "list"),
            response_model=List[item.model],
            # dependencies=[Annotated[Depends(item.get_input), Query()]],
            summary=f"List all {key}",
            operation_id=f"get_all_{key}",
            methods=["GET"],
            tags=[key],
        )
        # get one by pk
        # TODO: returning a single object, include related records
        app.add_api_route(
            f"/api/{key.lower()}/{"/".join([f"{{{pk}}}" for pk in pks])}",
            create_endpoint(key, "get_one"),
            response_model=item.model,
            summary=f"Get one {inflect.singular_noun(key)} by primary key",
            operation_id=f"get_one_{inflect.singular_noun(key)}",
            methods=["GET"],
            tags=[key],
        )
        # The POST method is used for creating data
        app.add_api_route(
            f"/api/{key.lower()}",
            create_endpoint(key, "create"),
            response_model=List[item.model],
            # dependencies=[Annotated[Depends(item.get_input), Query()]],
            summary=f"Create some {key}",
            operation_id=f"create_{key}",
            methods=["POST"],
            tags=[key],
        )
        # The PUT replace completely the resource
        # the PATCH method is used for partially updating a resource
        app.add_api_route(
            f"/api/{key.lower()}/{"/".join([f"{{{pk}}}" for pk in pks])}",
            create_endpoint(key, "update"),
            response_model=item.model,
            # dependencies=[Annotated[Depends(item.get_input), Query()]],
            summary=f"Update one {key}",
            operation_id=f"update_{key}",
            methods=["PATCH"],
            tags=[key],
        )
        # The DELETE method is used for removing data.
        app.add_api_route(
            f"/api/{key.lower()}/{"/".join([f"{{{pk}}}" for pk in pks])}",
            create_endpoint(key, "delete"),
            response_model=item.model,
            # dependencies=[Annotated[Depends(item.get_input), Query()]],
            summary=f"Delete one {key}",
            operation_id=f"delete_{key}",
            methods=["DELETE"],
            tags=[key],
        )
        # http://api.example.com/v1/store/items/{id}✅
        # http://api.example.com/v1/store/employees/{id}✅
        # http://api.example.com/v1/store/employees/{id}/addresses
        # /device-management/managed-devices/{id}/scripts/{id}/execute	//DON't DO THIS!
        # /device-management/managed-devices/{id}/scripts/{id}/status		//POST request with action=execute
        # _ protects keywords in pagination and advanced filtering
        # /api/books?_offset=0&_limit=10&_orderBy=author desc,title asc
        # basic FILTER on equality of fields
        # http://api.example.com/v1/store/items?group=124
        # http://api.example.com/v1/store/employees?department=IT&region=USA
        # advanced FILTER on multiple fields using expressions
        # /api/books?page=0&size=20&$filter=author eq 'Fitzgerald'
        # /api/books?page=0&size=20&$filter=(author eq 'Fitzgerald' or name eq 'Redmond') and price lt 2.55
        # /v1.0/people?$filter=name eq 'david'&$orderBy=hireDate
        # https://docs.oasis-open.org/odata/odata/v4.01/odata-v4.01-part2-url-conventions.html#_Toc31361038
