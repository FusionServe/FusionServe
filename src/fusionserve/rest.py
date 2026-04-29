"""REST API controller generation for FusionServe.

This module dynamically creates Litestar controllers for database tables
discovered via SQLAlchemy automap. Each controller exposes standard CRUD
endpoints (list, get, create, update, delete) with support for pagination,
field-level filtering, and OData-style advanced filters.
"""

import logging
from typing import Annotated, ClassVar

import litestar
import odata_query
import odata_query.exceptions
import odata_query.sqlalchemy
from advanced_alchemy.extensions.litestar import filters
from litestar import Request
from litestar.datastructures import State
from litestar.exceptions import ClientException, NotFoundException
from litestar.params import Dependency
from pydantic import BaseModel, ConfigDict, create_model
from pydantic.alias_generators import to_pascal
from sqlalchemy import Table, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.automap import AutomapBase
from sqlalchemy.orm import DeclarativeMeta

from . import auth
from .config import settings
from .di import create_filter_dependencies
from .models import AdvancedFilter
from .persistence import inflect, parse_comments, pydantic_field_from_column, set_role

_logger = logging.getLogger(settings.app_name)
# tags_metadata = []


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


# Litestar conversion starts here


def create_response_model(table: Table) -> type[BaseModel]:
    """Dynamically create a Pydantic response model for a database table.

    Generates a class named ``{PascalSingularTableName}Model`` with one field
    per column.  Nullability mirrors the column's ``nullable`` flag.

    Args:
        table: The SQLAlchemy ``Table`` whose columns drive the model fields.

    Returns:
        A Pydantic ``BaseModel`` subclass with ``from_attributes=True``,
        suitable for validating ORM rows returned from the database.
    """
    return create_model(
        to_pascal(f"{inflect.singular_noun(table.name)}_model"),
        __config__=ConfigDict(from_attributes=True),
        **{
            name: pydantic_field_from_column(column, "model")
            for name, column in table.columns.items()
            if pydantic_field_from_column(column, "model")[0]
        },
    )


def create_get_input_model(table: Table) -> type[BaseModel]:
    """Dynamically create a Pydantic model for field-equality query parameters.

    Generates a class named ``{PascalSingularTableName}GetInput`` with one
    optional field per column, used to express ``WHERE field = value``
    filters in the list endpoint's query string.

    Args:
        table: The SQLAlchemy ``Table`` whose columns drive the model fields.

    Returns:
        A Pydantic ``BaseModel`` subclass.
    """
    return create_model(
        to_pascal(f"{inflect.singular_noun(table.name)}_get_input"),
        __config__=ConfigDict(from_attributes=True),
        **{
            name: pydantic_field_from_column(column, "get_input")
            for name, column in table.columns.items()
            if pydantic_field_from_column(column, "get_input")[0]
        },
    )


def create_create_input_model(table: Table) -> type[BaseModel]:
    """Dynamically create a Pydantic model for ``POST`` request bodies.

    Generates a class named ``{PascalSingularTableName}CreateInput``. Columns
    with either a server-side default (``server_default``) or a Python-side
    default (``default``) become optional, so clients are not forced to
    provide values for surrogate keys, ``created_at`` timestamps, etc.
    Non-nullable columns without a default remain required.

    Args:
        table: The SQLAlchemy ``Table`` whose columns drive the model fields.

    Returns:
        A Pydantic ``BaseModel`` subclass.
    """
    return create_model(
        to_pascal(f"{inflect.singular_noun(table.name)}_create_input"),
        __config__=ConfigDict(from_attributes=True),
        **{
            name: pydantic_field_from_column(column, "create_input")
            for name, column in table.columns.items()
            if pydantic_field_from_column(column, "create_input")[0]
        },
    )


def create_controller(orm_class: DeclarativeMeta) -> litestar.Controller:
    """Dynamically create a Litestar Controller class for a given ORM class.

    Generates a ``Controller`` sub-class with five HTTP handlers: ``GET /``
    (list), ``GET /{pk}`` (retrieve), ``POST /`` (create), ``PATCH /{pk}``
    (update), and ``DELETE /{pk}`` (delete).  Pydantic response and query
    models are built on the fly from the ORM class's table — no external
    registry is required.

    Args:
        orm_class: The SQLAlchemy automap-generated ORM class representing
            the underlying table.

    Returns:
        A dynamically constructed :class:`litestar.Controller` subclass wired
        to the given table, ready to be mounted on a Litestar application.
    """
    table: Table = orm_class.__table__
    table_name = table.name
    pkeys = table.primary_key.columns.keys()
    comment = parse_comments(table)
    response_model = create_response_model(table)
    get_input_model = create_get_input_model(table)
    create_input_model = create_create_input_model(table)

    class ItemController(litestar.Controller):
        """Auto-generated CRUD controller for a single database table.

        The controller is parametrised at class-creation time via the enclosing
        ``create_controller`` closure and therefore handles exactly one table.
        All database access is performed through an injected
        :class:`sqlalchemy.ext.asyncio.AsyncSession`.
        """

        path = f"{settings.base_path}/{table_name}"
        dependencies = create_filter_dependencies(
            {
                "pagination_type": "limit_offset",
                "pagination_size": settings.default_page_size,
            }
        )
        tags: ClassVar[list[str]] = [f"{table.name}: {comment.content if comment.content else ''}"]

        @litestar.get(
            summary=f"List {table_name}",
            description=f"List {table_name}, filtering on any field using advanced filters, pagination and ordering",
            security=[{"BearerToken": []}],
        )
        async def list_items(
            self,
            session: AsyncSession,
            request: Request[auth.User, str, State],
            filters: Annotated[list[filters.FilterTypes], Dependency(skip_validation=True)],
            # order_by: filters.OrderBy ,
            condition: get_input_model | None = None,  # type: ignore
            advanced_filter: AdvancedFilter | None = None,
        ) -> list[response_model]:  # type: ignore
            """Return a paginated, optionally filtered list of records.

            Applies limit/offset pagination from *filters*, field-equality
            conditions from *condition*, and an OData ``$filter`` expression
            from *advanced_filter* (if provided).

            Args:
                session: The active async SQLAlchemy session injected by DI.
                filters: Limit/offset pagination and ordering filters supplied
                    by the ``advanced_alchemy`` filter dependency.
                condition: Optional Pydantic model whose non-``None`` fields are
                    translated into SQL ``WHERE field = value`` clauses.
                advanced_filter: Optional OData ``$filter`` expression string
                    wrapped in an :class:`.AdvancedFilter` model.

            Returns:
                A list of validated Pydantic model instances representing the
                matching rows.

            Raises:
                litestar.exceptions.ClientException: If *advanced_filter*
                    contains an invalid OData expression.
            """
            # TODO: user.role is set to the default role (first in the list) by auth machinery.
            # Update with the required role for the table retrieved from the Smart Comments;
            # raise 403 if the user is not authorized
            # TODO: what about exc?
            await set_role(session, request.user)
            limit_offset = filters[0]
            if limit_offset.limit > settings.max_page_size:
                raise ClientException(f"limit {limit_offset.limit} exceeds max_page_size {settings.max_page_size}")
            statement = limit_offset.append_to_statement(select(orm_class), orm_class)
            # statement = select(orm_class)
            if condition:
                for k in condition.model_fields:
                    # skip attributes not in query string
                    if getattr(condition, k):
                        # add the where condition to select expression
                        statement = statement.where(getattr(orm_class, k) == getattr(condition, k))
            if advanced_filter:
                try:
                    statement = odata_query.sqlalchemy.apply_odata_query(statement, advanced_filter.filter)
                except (
                    odata_query.exceptions.InvalidFieldException,
                    odata_query.exceptions.ParsingException,
                ) as e:
                    # TODO: standardize error responses as the best practises
                    _logger.error("Invalid filter: %s", e)
                    raise ClientException(f"Invalid filter: {e}") from e
            results = (await session.execute(statement)).scalars().all()
            return [response_model.model_validate(result) for result in results]

        @litestar.get(
            path=f"/{'/'.join([f'{{{pk}:uuid}}' for pk in pkeys])}",
            raises=[NotFoundException],
            summary=f"Get a {inflect.singular_noun(table_name)}",
            description=f"Get a {inflect.singular_noun(table_name)} by its primary key(s)",
            security=[{"BearerToken": []}],
        )
        async def get_item(
            self,
            session: AsyncSession,
            request: litestar.Request,
        ) -> response_model:  # type: ignore
            """Retrieve a single record by its primary key(s).

            Args:
                session: The active async SQLAlchemy session injected by DI.
                request: The current HTTP request; primary key values are read
                    from ``request.path_params``.

            Returns:
                A validated Pydantic model instance for the found record.

            Raises:
                litestar.exceptions.NotFoundException: If no record with the
                    given primary key(s) exists.
            """
            await set_role(session, request.user)
            record = await session.get(orm_class, request.path_params)
            if not record:
                raise NotFoundException(
                    f"No {inflect.singular_noun(table_name)} with id(s) {request.path_params} found"
                )
            return response_model.model_validate(record)

        @litestar.post(
            summary=f"Create a new {inflect.singular_noun(table_name)}",
            description=f"Create a new {inflect.singular_noun(table_name)}",
            security=[{"BearerToken": []}],
        )
        async def create_item(
            self,
            session: AsyncSession,
            request: litestar.Request,
            data: create_input_model,  # type: ignore
        ) -> response_model:  # type: ignore
            """Insert a new record into the database.

            Args:
                session: The active async SQLAlchemy session injected by DI.
                request: The current HTTP request; primary key values are read
                    from ``request.path_params``.
                data: A validated Pydantic model instance carrying the field
                    values for the new record.  ``None`` values are excluded
                    from the insert statement.

            Returns:
                The newly created record as a validated Pydantic model instance,
                refreshed from the database after commit.
            """
            await set_role(session, request.user)
            new_item = orm_class(**data.model_dump(exclude_none=True))
            session.add(new_item)
            await session.commit()
            await session.refresh(new_item)
            return new_item

        @litestar.patch(
            path=f"/{'/'.join([f'{{{pk}:uuid}}' for pk in pkeys])}",
            raises=[NotFoundException],
            summary=f"Update a {inflect.singular_noun(table_name)}",
            description=f"Update a {inflect.singular_noun(table_name)} by its primary key(s)",
            security=[{"BearerToken": []}],
        )
        async def update_item(
            self,
            session: AsyncSession,
            request: litestar.Request,
            data: response_model,  # type: ignore
        ) -> response_model:  # type: ignore
            """Partially update an existing record (PATCH semantics).

            Only fields that are explicitly set in *data* (i.e. present in the
            request body and not ``None``) are written to the database.

            Args:
                session: The active async SQLAlchemy session injected by DI.
                request: The current HTTP request; primary key values are read
                    from ``request.path_params``.
                data: A Pydantic model instance whose set, non-``None`` fields
                    override the corresponding columns on the existing record.

            Returns:
                The updated record as a validated Pydantic model instance.

            Raises:
                litestar.exceptions.NotFoundException: If no record with the
                    given primary key(s) exists.
            """
            await set_role(session, request.user)
            record = await session.get(orm_class, request.path_params)
            if not record:
                raise NotFoundException(
                    f"No {inflect.singular_noun(table_name)} with id(s) {request.path_params} found"
                )
            for k, v in data.model_dump(exclude_unset=True, exclude_none=True).items():
                setattr(record, k, v)
            session.add(record)
            await session.commit()
            return record

        @litestar.delete(
            path=f"/{'/'.join([f'{{{pk}:uuid}}' for pk in pkeys])}",
            raises=[NotFoundException],
            summary=f"Delete a {inflect.singular_noun(table_name)}",
            description=f"Delete a {inflect.singular_noun(table_name)} by its primary key(s)",
            security=[{"BearerToken": []}],
        )
        async def delete_item(
            self,
            session: AsyncSession,
            request: litestar.Request,
        ) -> None:
            """Delete a record identified by its primary key(s).

            Args:
                session: The active async SQLAlchemy session injected by DI.
                request: The current HTTP request; primary key values are read
                    from ``request.path_params``.

            Returns:
                ``None`` - HTTP 204 No Content is returned on success.

            Raises:
                litestar.exceptions.NotFoundException: If no record with the
                    given primary key(s) exists.
            """
            await set_role(session, request.user)
            record = await session.get(orm_class, request.path_params)
            if not record:
                raise NotFoundException(
                    f"No {inflect.singular_noun(table_name)} with id(s) {request.path_params} found"
                )
            await session.delete(record)
            await session.commit()

    return ItemController


def build(_base: AutomapBase) -> list[litestar.Controller]:
    """Build and return a list of Litestar controllers for every reflected table.

    Iterates over ``_base.classes`` and calls :func:`create_controller` for
    each ORM class.  No external registry is required: response and query
    Pydantic models are derived from each table at controller-creation time.

    Args:
        _base: The SQLAlchemy automap base whose ``.classes`` attribute maps
            table names to ORM classes.

    Returns:
        A list of dynamically generated :class:`litestar.Controller` subclasses,
        one per table in ``_base.classes``.
    """
    return [create_controller(orm_class) for orm_class in _base.classes]
