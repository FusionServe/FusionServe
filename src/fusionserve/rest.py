"""REST API controller generation for FusionServe.

This module dynamically creates Litestar controllers for database tables
discovered via SQLAlchemy automap. Each controller exposes standard CRUD
endpoints (list, get, create, update, delete) with support for pagination,
field-level filtering, and OData-style advanced filters.
"""

import logging
from typing import Annotated, Any, ClassVar

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
from sqlalchemy import Table, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.automap import AutomapBase
from sqlalchemy.orm import DeclarativeMeta
from strawberry.scalars import JSON as StrawberryJSON

from . import auth
from .config import settings
from .di import create_filter_dependencies
from .models import AdvancedFilter, CrudAction, FunctionInfo, FunctionReturnKind, Introspection, SmartComment
from .persistence import async_session, inflect, pydantic_field_from_column, set_role

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


def create_controller(orm_class: DeclarativeMeta, is_view: bool = False) -> litestar.Controller:
    """Dynamically create a Litestar Controller class for a given ORM class.

    Generates a ``Controller`` sub-class with five HTTP handlers: ``GET /``
    (list), ``GET /{pk}`` (retrieve), ``POST /`` (create), ``PATCH /{pk}``
    (update), and ``DELETE /{pk}`` (delete).  Pydantic response and query
    models are built on the fly from the ORM class's table — no external
    registry is required.

    When ``is_view`` is ``True`` the three write handlers (create, update,
    delete) are dropped, leaving a read-only controller, because views are
    generally not writable.

    Args:
        orm_class: The SQLAlchemy automap-generated ORM class representing
            the underlying table.
        is_view: Whether ``orm_class`` is backed by a view; if so, only the
            read handlers are kept.

    Returns:
        A dynamically constructed :class:`litestar.Controller` subclass wired
        to the given table, ready to be mounted on a Litestar application.
    """
    table: Table = orm_class.__table__
    table_name = table.name
    pkeys = table.primary_key.columns.keys()
    comment = SmartComment.from_object(table)
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

        path = f"{settings.base_path}/v1/{table_name}"
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

    if is_view:
        # Views are read-only: strip the write handlers before the controller
        # is registered so only list + retrieve routes are mounted.
        del ItemController.create_item
        del ItemController.update_item
        del ItemController.delete_item

    # Smart-comment ``exclude`` directive: drop the handlers for each suppressed
    # CRUD action. ``READ`` removes both the list and the get-by-pk endpoints.
    # ``hasattr`` guards keep this composable with the ``is_view`` strip above.
    excluded = comment.excluded
    handlers_by_action = {
        CrudAction.READ: ("list_items", "get_item"),
        CrudAction.CREATE: ("create_item",),
        CrudAction.UPDATE: ("update_item",),
        CrudAction.DELETE: ("delete_item",),
    }
    for action in excluded:
        for handler_name in handlers_by_action[action]:
            if hasattr(ItemController, handler_name):
                delattr(ItemController, handler_name)

    return ItemController


def build(introspection: Introspection) -> list[litestar.Controller]:
    """Build and return a list of Litestar controllers for every reflected table.

    Iterates over ``introspection.base.classes`` and calls
    :func:`create_controller` for each ORM class.  No external registry is
    required: response and query Pydantic models are derived from each table at
    controller-creation time. Classes whose table name is in
    ``introspection.views`` get a read-only controller.

    Args:
        introspection: The :class:`Introspection` returned by
            :func:`fusionserve.persistence.introspect`, providing the automap
            base and the set of mapped read-only view names.

    Returns:
        A list of dynamically generated :class:`litestar.Controller` subclasses,
        one per table in ``introspection.base.classes``. A table whose
        smart-comment ``exclude`` directive suppresses every applicable action
        (e.g. ``exclude: true``) is dropped entirely so no empty controller is
        mounted.
    """
    controllers: list[litestar.Controller] = []
    for orm_class in introspection.base.classes:
        is_view = orm_class.__table__.name in introspection.views
        # Views only ever expose reads; tables expose the full CRUD set. Skip
        # the controller when every applicable action is excluded.
        applicable = {CrudAction.READ} if is_view else set(CrudAction)
        if applicable <= SmartComment.from_object(orm_class.__table__).excluded:
            continue
        controllers.append(create_controller(orm_class, is_view))
    return controllers


def _python_type_for_rest(t: type) -> Any:
    """Translate a function param/return Python type into a Pydantic-friendly type.

    The custom-query introspection map uses :data:`strawberry.scalars.JSON` for
    PostgreSQL ``json`` / ``jsonb`` so that GraphQL gets a lossless JSON
    scalar. Pydantic doesn't understand that NewType wrapper; for the REST
    surface we map it back to ``Any`` (Pydantic accepts arbitrary JSON
    natively). Every other type passes through unchanged.

    Args:
        t: The Python type to adapt.

    Returns:
        ``Any`` if ``t`` is the Strawberry ``JSON`` scalar, otherwise ``t``.
    """
    if t is StrawberryJSON:
        return Any
    return t


def _scalar_response_model(fn: FunctionInfo) -> type[BaseModel]:
    """Build the ``{"value": <scalar>}`` Pydantic response model for a SCALAR function.

    Args:
        fn: The function metadata; ``fn.return_python_type`` must be set.

    Returns:
        A Pydantic model with a single ``value`` field whose type matches the
        function's mapped scalar return type.
    """
    return create_model(
        f"{to_pascal(fn.name)}ScalarResponse",
        __config__=ConfigDict(from_attributes=True),
        value=(_python_type_for_rest(fn.return_python_type) | None, None),
    )


def _function_argument_dependencies(fn: FunctionInfo) -> dict[str, type | Any]:
    """Build a Litestar handler signature dict for a function's IN parameters.

    Returns a mapping of ``{param_name: type_annotation}`` suitable for
    splatting into a dynamically-created handler. Optional (``has_default``)
    parameters are typed as ``T | None`` and default to ``None``; required
    ones are typed as ``T`` with no default. Splatting this mapping into
    function-creation machinery is currently NOT used directly — instead the
    handler reads parameters from the ``Request.query_params`` mapping at
    runtime — but this helper is exposed for future use where Litestar's
    typed query-parameter dependency injection is desired.

    Args:
        fn: The function metadata.

    Returns:
        Mapping of parameter name to its REST-side Python type annotation.
    """
    return {p.name: _python_type_for_rest(p.python_type) for p in fn.params}


def _coerce_query_param(raw: str, python_type: type) -> Any:
    """Best-effort coercion of a query-string value to the declared Python type.

    Litestar would normally do this automatically when handler parameters are
    typed, but the function-controller handlers receive parameters via the
    raw query-params mapping (the param set is dynamic per function). This
    helper applies a minimal, well-defined coercion so the resulting bind
    value round-trips correctly through asyncpg.

    Args:
        raw: The raw string value from the query string.
        python_type: The target Python type from
            :data:`fusionserve.persistence._PG_TO_PY` (or ``Any``).

    Returns:
        The coerced value, or ``raw`` unchanged if no coercion rule matches.

    Raises:
        litestar.exceptions.ClientException: If the value cannot be coerced
            to the declared type.
    """
    if python_type in (str, Any) or python_type is StrawberryJSON:
        return raw
    try:
        if python_type is int:
            return int(raw)
        if python_type is float:
            return float(raw)
        if python_type is bool:
            lowered = raw.lower()
            if lowered in ("true", "1", "yes", "on"):
                return True
            if lowered in ("false", "0", "no", "off"):
                return False
            raise ValueError(f"cannot interpret {raw!r} as bool")
        # Fallback: instantiate the type from the raw string. Works for
        # ``Decimal``, ``uuid.UUID``, and the ``datetime`` classes (which all
        # parse ISO-style strings via their ``fromisoformat`` constructors).
        if hasattr(python_type, "fromisoformat"):
            return python_type.fromisoformat(raw)
        return python_type(raw)
    except (ValueError, TypeError) as exc:
        raise ClientException(f"Invalid value for parameter: {exc}") from exc


def _build_function_sql(fn: FunctionInfo, supplied_params: list[str]) -> str:
    """Compose the SQL statement that invokes ``fn`` with the supplied named args.

    Uses PostgreSQL's named-argument call syntax (``arg := :arg``) so that any
    parameters omitted by the caller fall back to the function's declared
    PostgreSQL ``DEFAULT``.

    Args:
        fn: The function metadata.
        supplied_params: Names of the parameters the caller actually sent
            (i.e. the keys of the bind mapping). Schema/function names are
            sourced from validated introspection metadata, never from
            client-controlled input.

    Returns:
        A SQL string with named placeholders for every supplied argument.
    """
    qualified = f'"{fn.schema}"."{fn.name}"'
    placeholders = ", ".join(f"{name} := :{name}" for name in supplied_params)
    if fn.return_kind == FunctionReturnKind.SCALAR:
        return f"SELECT {qualified}({placeholders}) AS value"
    return f"SELECT * FROM {qualified}({placeholders})"


def create_function_controller(
    fn: FunctionInfo,
    base: AutomapBase,
) -> type[litestar.Controller]:
    """Dynamically create a Litestar Controller for one custom-query function.

    Mounts ``GET {settings.base_path}/v1/{fn.name}``. Arguments are read from the
    query string and coerced to the declared Python type per
    :func:`_coerce_query_param`. The handler:

    * opens an :func:`async_session`,
    * applies the per-request PostgreSQL role via
      :func:`fusionserve.persistence.set_role`,
    * issues the function call with named-argument PG syntax so server-side
      defaults are honoured for omitted arguments,
    * shapes the response per :class:`fusionserve.models.FunctionReturnKind`.

    Args:
        fn: The function metadata.
        base: The SQLAlchemy automap base — needed to look up the ORM class
            for ``ROW`` / ``SET`` returns and build their Pydantic response
            models.

    Returns:
        A dynamically constructed :class:`litestar.Controller` subclass.
    """
    description = fn.description or f"Custom query exposing {fn.schema}.{fn.name}()."

    if fn.return_kind == FunctionReturnKind.SCALAR:
        response_model: type[BaseModel] = _scalar_response_model(fn)
        return_annotation: Any = response_model
    else:
        orm_class = base.classes.get(fn.return_table_name)
        table: Table = orm_class.__table__
        response_model = create_model(
            f"{to_pascal(inflect.singular_noun(table.name))}Model",
            __config__=ConfigDict(from_attributes=True),
            **{
                name: pydantic_field_from_column(column, "model")
                for name, column in table.columns.items()
                if pydantic_field_from_column(column, "model")[0]
            },
        )
        return_annotation = list[response_model] if fn.return_kind == FunctionReturnKind.SET else response_model

    fn_for_handler = fn

    class FunctionController(litestar.Controller):
        """Auto-generated controller wrapping a single STABLE/IMMUTABLE PG function."""

        path = f"{settings.base_path}/v1/{fn_for_handler.name}"
        tags: ClassVar[list[str]] = [f"functions: {fn_for_handler.name}"]

        @litestar.get(
            summary=f"Call {fn_for_handler.name}",
            description=description,
            security=[{"BearerToken": []}],
        )
        async def call(  # type: ignore[no-redef]
            self,
            session: AsyncSession,
            request: Request[auth.User, str, State],
        ) -> return_annotation:  # type: ignore[valid-type]
            """Invoke the underlying PostgreSQL function and return its result.

            Args:
                session: The active async SQLAlchemy session injected by DI.
                request: The current HTTP request; query parameters carry the
                    function arguments.

            Returns:
                For SCALAR returns: a ``{"value": <scalar>}`` JSON object.
                For ROW returns: the matching Pydantic model instance, or 404
                if the function returned no row.
                For SET returns: a list of Pydantic model instances (possibly
                empty).

            Raises:
                litestar.exceptions.ClientException: If a query parameter
                    cannot be coerced to its declared Python type.
                litestar.exceptions.NotFoundException: If a ROW return yields
                    no row.
            """
            bind: dict[str, Any] = {}
            for param in fn_for_handler.params:
                raw = request.query_params.get(param.name)
                if raw is None:
                    if not param.has_default:
                        raise ClientException(f"Missing required parameter {param.name!r}")
                    continue
                bind[param.name] = _coerce_query_param(raw, param.python_type)

            sql = _build_function_sql(fn_for_handler, list(bind.keys()))
            statement = text(sql).bindparams(**bind)

            async with async_session() as call_session:
                await set_role(call_session, request.user)
                if fn_for_handler.return_kind == FunctionReturnKind.SCALAR:
                    value = (await call_session.execute(statement)).scalar()
                    return response_model.model_validate({"value": value})
                if fn_for_handler.return_kind == FunctionReturnKind.SET:
                    rows = (await call_session.execute(statement)).mappings().all()
                    return [response_model.model_validate(dict(r)) for r in rows]
                # ROW
                row = (await call_session.execute(statement)).mappings().first()
                if row is None:
                    raise NotFoundException(f"Function {fn_for_handler.schema}.{fn_for_handler.name}() returned no row")
                return response_model.model_validate(dict(row))

    return FunctionController


def build_function_controllers(introspection: Introspection) -> list[type[litestar.Controller]]:
    """Build one Litestar controller per supported PG function in ``introspection``.

    Skips a function (with a logged warning) if its REST path collides with
    an already-registered table path or with another function path. The
    table-driven path always wins.

    Args:
        introspection: The :class:`Introspection` returned by
            :func:`fusionserve.persistence.introspect`.

    Returns:
        A list of dynamically constructed controller classes ready to be
        mounted on the Litestar application.
    """
    controllers: list[type[litestar.Controller]] = []

    table_paths = {f"{settings.base_path}/v1/{name}" for name in introspection.base.classes}
    # Also reserve the per-pk paths so a function named identically to
    # ``/{pk}`` (after slug-collapsing) cannot collide with a row-detail
    # endpoint. The set is consulted by the collision check below.
    for orm_class in introspection.base.classes:
        for pk in orm_class.__table__.primary_key.columns:
            table_paths.add(f"{settings.base_path}/v1/{orm_class.__table__.name}/{{{pk}}}")
    used_paths: set[str] = set()

    for fn in introspection.functions:
        path = f"{settings.base_path}/v1/{fn.name}"
        if path in table_paths or path in used_paths:
            # TODO: collision-resolution strategy. Options under consideration:
            #   * prefix function paths with ``/rpc`` or ``/fn``
            #   * suffix with arity (impossible since overloads are pre-rejected)
            #   * allow function to win when explicitly opted-in via smart-comment metadata
            # For v1: existing table path wins, function is dropped.
            _logger.warning(
                "Skipping REST exposure of function %s.%s: path %s collides with an existing route.",
                fn.schema,
                fn.name,
                path,
            )
            continue
        controllers.append(create_function_controller(fn, introspection.base))
        used_paths.add(path)
    return controllers
