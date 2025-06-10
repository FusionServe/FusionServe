import logging
from typing import Annotated, List

import inflect as _inflect
import odata_query
import odata_query.exceptions
import odata_query.sqlalchemy
from fastapi import APIRouter, Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import Table, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.automap import AutomapBase
from sqlalchemy.orm import DeclarativeMeta

from .models import AdvancedFilter, PaginationParams
from .persistence import get_async_session, set_role
from .config import settings

_logger = logging.getLogger(settings.app_name)

inflect = _inflect.engine()
inflect.classical(names=0)
# tags_metadata = []


def create_endpoint(table_name: str, endpoint_type: str):
    endpoint = {}
    orm_class: DeclarativeMeta = Base.classes.get(table_name)
    if endpoint_type == "list":
        get_input = models_registry[table_name].get_input

        async def endpoint(  # noqa: F811
            # request: Request,
            condition: Annotated[get_input, Query(), Depends()],  # type: ignore
            pagination: Annotated[PaginationParams, Query(), Depends(PaginationParams)] = None,
            advanced_filter: Annotated[AdvancedFilter, Query(), Depends()] = None,
            session: AsyncSession = Depends(get_async_session),
        ):
            await set_role(session)
            statement = (
                select(orm_class).limit(pagination.limit).offset(pagination.offset)
            )
            for k in condition.model_fields:
                # skip attributes not in query string
                if getattr(condition, k):
                    # add the where condition to select expression
                    statement = statement.where(
                        getattr(orm_class, k) == getattr(condition, k)
                    )
            try:
                if advanced_filter.filter:
                    statement = odata_query.sqlalchemy.apply_odata_query(
                        statement, advanced_filter.filter
                    )
            except (
                odata_query.exceptions.InvalidFieldException,
                odata_query.exceptions.ParsingException,
            ) as e:
                # TODO: standardize error responses as the best practises
                _logger.error(f"Invalid filter: {e}")
                return JSONResponse({"error": str(e)})
            results = (await session.execute(statement)).scalars().all()
            return results

    if endpoint_type == "get_one":
        pk_input = models_registry[table_name].pk_input

        async def endpoint(  # noqa: F811
            request: Request,
            pk: Annotated[pk_input, Depends()],  # type: ignore
            session: AsyncSession = Depends(get_async_session),
        ):
            await set_role(session)
            return await session.get(orm_class, pk.model_dump())

    if endpoint_type == "create":
        # TODO: create a specific input model with required fields(Not nulls w/o defaults)
        create_input = models_registry[table_name].get_input

        async def endpoint(  # noqa: F811
            # request: Request,
            input: List[create_input],  # type: ignore
            session: AsyncSession = Depends(get_async_session),
        ):
            await set_role(session)
            try:
                results = (
                    (
                        await session.execute(
                            insert(orm_class).returning(orm_class),
                            input,
                        )
                    )
                    .scalars()
                    .all()
                )
            except IntegrityError as e:
                # TODO: standardize error responses as the best practises
                lines = str(e.orig).splitlines()
                return JSONResponse(
                    {
                        "error": lines[0].split(":")[1].strip(),
                        "detail": lines[1].split(":")[1].strip(),
                    }
                )
            await session.commit()
            return results

    # TODO: is replace really needed?

    if endpoint_type == "update":
        pk_input = models_registry[table_name].pk_input
        # TODO: create a specific input model with required fields(Not nulls w/o defaults)
        update_input = models_registry[table_name].get_input

        async def endpoint(  # noqa: F811
            # request: Request,
            pk: Annotated[pk_input, Depends()],  # type: ignore
            input: update_input,  # type: ignore
            session: AsyncSession = Depends(get_async_session),
        ):
            await set_role(session)
            try:
                item = await session.get(orm_class, pk.model_dump())
                for k, v in input.model_dump(
                    exclude_unset=True, exclude_none=True
                ).items():
                    setattr(item, k, v)
                session.add(item)
            except IntegrityError as e:
                # TODO: standardize error responses as the best practises
                _logger.error(f"Integrity error: {e}")
                lines = str(e.orig).splitlines()
                return JSONResponse(
                    {
                        "error": lines[0].split(":")[1].strip(),
                        "detail": lines[1].split(":")[1].strip(),
                    }
                )
            await session.commit()
            return item

    if endpoint_type == "delete":
        pk_input = models_registry[table_name].pk_input

        async def endpoint(  # noqa: F811
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
                _logger.error(f"Integrity error: {e}")
                lines = str(e.orig).splitlines()
                return JSONResponse(
                    {
                        "error": lines[0].split(":")[1].strip(),
                        "detail": lines[1].split(":")[1].strip(),
                    }
                )
            await session.commit()
            return item

    return endpoint


def build(_base: AutomapBase, _registry):
    global Base, models_registry
    Base = _base
    models_registry = _registry
    router = APIRouter()
    # generate fancy tags descriptions from table comments
    router.openapi_tags = []
    for key, item in models_registry.items():
        table: Table = Base.classes.get(key).__table__
        router.openapi_tags.append({"name": table.name, "description": table.comment})
        pks = table.primary_key.columns.keys()
        # list
        router.add_api_route(
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
        router.add_api_route(
            f"/api/{key.lower()}/{"/".join([f"{{{pk}}}" for pk in pks])}",
            create_endpoint(key, "get_one"),
            response_model=item.model,
            summary=f"Get one {inflect.singular_noun(key)} by primary key",
            operation_id=f"get_one_{inflect.singular_noun(key)}",
            methods=["GET"],
            tags=[key],
        )
        # The POST method is used for creating data
        router.add_api_route(
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
        router.add_api_route(
            f"/api/{key.lower()}/{"/".join([f"{{{pk}}}" for pk in pks])}",
            create_endpoint(key, "update"),
            response_model=item.model,
            # dependencies=[Annotated[Depends(item.get_input), Query()]],
            summary=f"Update one {inflect.singular_noun(key)}",
            operation_id=f"update_{inflect.singular_noun(key)}",
            methods=["PATCH"],
            tags=[key],
        )
        # The DELETE method is used for removing data.
        router.add_api_route(
            f"/api/{key.lower()}/{"/".join([f"{{{pk}}}" for pk in pks])}",
            create_endpoint(key, "delete"),
            response_model=item.model,
            # dependencies=[Annotated[Depends(item.get_input), Query()]],
            summary=f"Delete one {inflect.singular_noun(key)}",
            operation_id=f"delete_{inflect.singular_noun(key)}",
            methods=["DELETE"],
            tags=[key],
        )
    return router
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
