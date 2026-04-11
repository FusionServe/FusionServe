"""REST API controller generation for FusionServe.

This module dynamically creates Litestar controllers for database tables
discovered via SQLAlchemy automap. Each controller exposes standard CRUD
endpoints (list, get, create, update, delete) with support for pagination,
field-level filtering, and OData-style advanced filters.
"""

import logging
from typing import Annotated

import inflect as _inflect
import litestar
import odata_query
import odata_query.exceptions
import odata_query.sqlalchemy
from advanced_alchemy.extensions.litestar import filters

# from litestar import Controller, Request, delete, get, patch, post
from litestar.exceptions import ClientException, NotFoundException
from litestar.params import Dependency
from sqlalchemy import Table, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.automap import AutomapBase
from sqlalchemy.orm import DeclarativeMeta

from .config import settings
from .di import create_filter_dependencies
from .models import AdvancedFilter, RegistryItem
from .persistence import parse_comments, set_role

_logger = logging.getLogger(settings.app_name)

inflect = _inflect.engine()
inflect.classical(names=0)
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


def create_controller(table_name: str, item: any) -> litestar.Controller:
    """Dynamically create a Litestar Controller class for a given database table.

    Introspects the ORM class corresponding to *table_name* and generates a
    ``Controller`` sub-class with five HTTP handlers: ``GET /`` (list),
    ``GET /{pk}`` (retrieve), ``POST /`` (create), ``PATCH /{pk}`` (update),
    and ``DELETE /{pk}`` (delete).

    Args:
        table_name: The name of the database table (and URL path segment) for
            which the controller is generated.
        item: A registry entry object that exposes at least an ``item.model``
            Pydantic model used for serialisation/deserialisation and an
            ``item.get_input`` model used for field-level query parameters.

    Returns:
        A dynamically constructed :class:`litestar.Controller` subclass wired
        to the given table, ready to be mounted on a Litestar application.
    """
    orm_class: DeclarativeMeta = Base.classes.get(table_name)
    table: Table = orm_class.__table__
    pkeys = table.primary_key.columns.keys()
    comment = parse_comments(table)

    class ItemController(litestar.Controller):
        """Auto-generated CRUD controller for a single database table.

        The controller is parametrised at class-creation time via the enclosing
        ``create_controller`` closure and therefore handles exactly one table.
        All database access is performed through an injected
        :class:`sqlalchemy.ext.asyncio.AsyncSession`.
        """

        path = f"{settings.base_path}/{table_name}"
        dependencies = create_filter_dependencies({"pagination_type": "limit_offset"})
        tags = [f"{table.name}: {comment.content if comment.content else ''}"]
        get_input = models_registry[table_name].get_input

        @litestar.get(
            summary=f"List {table_name}",
            description=f"List {table_name}, filtering on any field using advanced filters, pagination and ordering",
        )
        async def list_items(
            self,
            session: AsyncSession,
            filters: Annotated[list[filters.FilterTypes], Dependency(skip_validation=True)],
            # order_by: filters.OrderBy ,
            condition: get_input | None = None,  # type: ignore
            advanced_filter: AdvancedFilter | None = None,
        ) -> list[item.model]:  # type: ignore
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
            await set_role(session)
            statement = filters[0].append_to_statement(select(orm_class), orm_class)
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
                    _logger.error(f"Invalid filter: {e}")
                    raise ClientException(f"Invalid filter: {e}") from e
            results = (await session.execute(statement)).scalars().all()
            return [item.model.model_validate(result) for result in results]

        @litestar.get(
            path=f"/{'/'.join([f'{{{pk}:uuid}}' for pk in pkeys])}",
            raises=[NotFoundException],
            summary=f"Get a {inflect.singular_noun(table_name)}",
            description=f"Get a {inflect.singular_noun(table_name)} by its primary key(s)",
        )
        async def get(
            self,
            session: AsyncSession,
            request: litestar.Request,
        ) -> item.model:  # type: ignore
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
            await set_role(session)
            record = await session.get(orm_class, request.path_params)
            if not record:
                raise NotFoundException(
                    f"No {inflect.singular_noun(table_name)} with id(s) {request.path_params} found"
                )
            return item.model.model_validate(record)

        @litestar.post(
            summary=f"Create a new {inflect.singular_noun(table_name)}",
            description=f"Create a new {inflect.singular_noun(table_name)}",
        )
        async def create(self, session: AsyncSession, data: item.model) -> item.model:  # type: ignore
            """Insert a new record into the database.

            Args:
                session: The active async SQLAlchemy session injected by DI.
                data: A validated Pydantic model instance carrying the field
                    values for the new record.  ``None`` values are excluded
                    from the insert statement.

            Returns:
                The newly created record as a validated Pydantic model instance,
                refreshed from the database after commit.
            """
            await set_role(session)
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
        )
        async def update(
            self,
            session: AsyncSession,
            request: litestar.Request,
            data: item.model,  # type: ignore
        ) -> item.model:  # type: ignore
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
            await set_role(session)
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
        )
        async def delete(
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
                ``None`` – HTTP 204 No Content is returned on success.

            Raises:
                litestar.exceptions.NotFoundException: If no record with the
                    given primary key(s) exists.
            """
            await set_role(session)
            record = await session.get(orm_class, request.path_params)
            if not record:
                raise NotFoundException(
                    f"No {inflect.singular_noun(table_name)} with id(s) {request.path_params} found"
                )
            await session.delete(record)
            await session.commit()

    return ItemController


def build_controllers(_base: AutomapBase, _registry: dict[str, RegistryItem]) -> list[litestar.Controller]:
    """Build and return a list of Litestar controllers for every registered table.

    Iterates over *_registry*, calls :func:`create_controller` for each entry,
    and collects the resulting controller classes.  Also sets the module-level
    ``Base`` and ``models_registry`` globals so that the dynamically defined
    inner classes can reference them at class-body evaluation time.

    Args:
        _base: The SQLAlchemy automap base whose ``.classes`` attribute maps
            table names to ORM classes.
        _registry: A mapping of table name → model registry entry.  Each entry
            must expose at minimum a ``model`` Pydantic class and a
            ``get_input`` Pydantic class.

    Returns:
        A list of dynamically generated :class:`litestar.Controller` subclasses,
        one per table in *_registry*.
    """
    global Base, models_registry
    Base = _base
    models_registry = _registry
    controllers: list[litestar.Controller] = []
    for key, item in models_registry.items():
        controller = create_controller(key, item)
        controllers.append(controller)
    return controllers
