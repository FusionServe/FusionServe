"""GraphQL schema generation for FusionServe — strawberry-orm spike.

This is the spike rewrite of the GraphQL builder on top of
``strawberry-orm`` (the backend-agnostic successor to
``strawberry-sqlalchemy-mapper``). It builds the schema dynamically from the
SQLAlchemy automap classes produced by :func:`fusionserve.persistence.introspect`.

Scope of the spike (see ``docs/superpowers/specs/2026-06-04-strawberry-orm-spike-*``):

* **Read path** (this module): per-table list + primary-key queries, native
  ``orm.filter`` / ``orm.order`` shapes, relationship traversal via the
  optimizer, and per-request row-level security.
* **Write path**: added in Phase 4 (mutations).
* **PG-function custom queries**: out of scope for the spike.

Key design points:

* Column fields are annotated with their concrete Python types (not
  :data:`strawberry_orm.auto`) so UUID / Decimal / datetime survive instead of
  being downgraded to ``str`` by the backend's type map.
* Bidirectional relationships are handled by pre-creating bare type classes in
  a shared synthetic module, pre-seeding ``__orm_filter__`` / ``__orm_order__``
  so the backend's relation wiring resolves cyclic references, then decorating
  each class in place.
* A single per-request :class:`~sqlalchemy.ext.asyncio.AsyncSession` carries the
  PostgreSQL role (via :func:`fusionserve.persistence.set_role`). The
  strawberry-orm SQLAlchemy backend reuses exactly the session returned by the
  ``session_getter``, so every query, optimizer eager-load, and nested relation
  load runs under the request's role.
"""

from __future__ import annotations

import logging
import sys
import types as _types_mod
from collections.abc import AsyncGenerator
from typing import Any

import litestar
import litestar.datastructures
import strawberry
from pydantic.alias_generators import to_pascal
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from strawberry.annotation import StrawberryAnnotation
from strawberry.extensions import QueryDepthLimiter
from strawberry.litestar import (
    BaseContext,
    HTTPContextType,
    WebSocketContextType,
    make_graphql_controller,
)
from strawberry.types.arguments import StrawberryArgument
from strawberry_orm import StrawberryORM

from . import auth
from .config import settings
from .models import Introspection, RecordNotFoundError
from .persistence import async_session, inflect, set_role

_logger = logging.getLogger(settings.app_name)

# Maximum recursion depth for nested where clauses (maps to the backend's
# ``max_filter_depth``); mirrors the previous implementation's guard.
_MAX_WHERE_DEPTH = 10

#: Name of the synthetic module that holds dynamically generated GraphQL type
#: classes, so cyclic relationship annotations resolve via normal name lookup.
_GENERATED_MODULE = "fusionserve._graphql_generated"


class CustomContext(BaseContext, kw_only=True):
    """Per-request Strawberry context carrying the role-scoped DB session.

    Attributes:
        session: The request's :class:`~sqlalchemy.ext.asyncio.AsyncSession`,
            opened by :func:`custom_context_getter` with the authenticated
            user's PostgreSQL role already applied. The strawberry-orm
            SQLAlchemy backend reuses this exact session for every query and
            nested relation load, so row-level security stays consistent.
    """

    session: AsyncSession


class CustomHTTPContextType(HTTPContextType, CustomContext):
    """HTTP context combining Litestar HTTP context with the custom context."""

    request: litestar.Request[auth.User, Any, litestar.datastructures.State]


class CustomWSContextType(WebSocketContextType, CustomContext):
    """WebSocket context combining Litestar WS context with the custom context."""

    socket: litestar.WebSocket[auth.User, Any, litestar.datastructures.State]


async def custom_context_getter(request: litestar.Request) -> AsyncGenerator[CustomContext]:
    """Open one role-scoped async session per GraphQL request.

    A single :class:`~sqlalchemy.ext.asyncio.AsyncSession` is opened and the
    request user's PostgreSQL role is applied via
    :func:`fusionserve.persistence.set_role` before any resolver runs. The
    session is exposed on the context so the strawberry-orm backend's
    ``session_getter`` can reuse it for the whole request, then closed when the
    request completes.

    Strawberry's Litestar integration wires this via ``litestar.di.Provide``,
    which honours async-generator dependencies — so yielding the context and
    closing the session in ``finally`` gives correct per-request lifecycle
    without leaking pooled connections.

    Args:
        request: The incoming Litestar HTTP request.

    Yields:
        A :class:`CustomContext` wrapping the role-scoped session.
    """
    session = async_session()
    await set_role(session, request.user)
    try:
        yield CustomContext(session=session)
    finally:
        await session.close()


def _session_from_context(info: strawberry.Info) -> AsyncSession:
    """Return the per-request role-scoped session for the strawberry-orm backend."""
    return info.context.session


@strawberry.experimental.pydantic.type(model=auth.User, all_fields=True, description="The authenticated user from JWT")
class JWTUser:
    """GraphQL projection of the authenticated :class:`fusionserve.auth.User`."""


def _gql_type_name(orm_class: type) -> str:
    """Return the singular PascalCase GraphQL type name for an automap class."""
    table_name = orm_class.__table__.name
    return to_pascal(inflect.singular_noun(table_name) or table_name)


def _column_annotation(column: Any) -> Any:
    """Return the concrete Python annotation for a column.

    Uses the column's ``python_type`` (so UUID / Decimal / datetime survive
    instead of being downgraded to ``str`` by the backend's type map), wrapping
    it in ``| None`` when the column is nullable. Columns whose type has no
    Python equivalent (e.g. JSON) fall back to :data:`strawberry_orm.auto`.
    """
    try:
        python_type = column.type.python_type
    except NotImplementedError:
        from strawberry_orm import auto

        return auto
    return python_type | None if column.nullable else python_type


def _set_resolver_arguments(field, arguments: list[StrawberryArgument]) -> None:
    """Replace a Strawberry field's resolver argument list in place."""
    field.base_resolver.arguments = arguments


def build(introspection: Introspection):
    """Build a strawberry-orm GraphQL controller from an :class:`Introspection`.

    Read-path only (spike Phase 2/3): per-table list and primary-key queries,
    native filter/order inputs, relationship traversal via the optimizer, and
    per-request row-level security.

    Args:
        introspection: The introspection result produced by
            :func:`fusionserve.persistence.introspect`.

    Returns:
        A Litestar-compatible GraphQL controller ready to be mounted.
    """
    orm = StrawberryORM.for_sqlalchemy(
        dialect="postgresql",
        session_getter=_session_from_context,
        default_query_limit=settings.default_page_size,
        max_filter_depth=_MAX_WHERE_DEPTH,
    )
    base = introspection.base
    orm_classes = list(base.classes)

    # ---- Pass 1: register filter + order types for every model. ----
    # The backend keys these in per-instance registries; relation (`object`)
    # traversal only wires a relation if the related model's filter/order is
    # already registered, so register them all before building any type.
    for orm_class in orm_classes:
        orm.filter(orm_class)
        orm.order(orm_class)

    # ---- Pre-create bare type classes in a shared module. ----
    # Cyclic relationships (e.g. author.books / book.author) mean one direction
    # always references a not-yet-decorated type. Creating all bare classes up
    # front (and pre-seeding their orm metadata) lets the backend's relation
    # wiring resolve every reference; ``strawberry.type`` then decorates each
    # class in place, so the annotations already point at the final types.
    generated_module = _types_mod.ModuleType(_GENERATED_MODULE)
    sys.modules[_GENERATED_MODULE] = generated_module
    bare_types: dict[type, type] = {}
    for orm_class in orm_classes:
        name = _gql_type_name(orm_class)
        cls = type(name, (), {})
        cls.__module__ = _GENERATED_MODULE
        cls.__orm_model__ = orm_class
        cls.__orm_filter__ = orm.backend._filter_registry.get(orm_class)
        cls.__orm_order__ = orm.backend._order_registry.get(orm_class)
        setattr(generated_module, name, cls)
        bare_types[orm_class] = cls

    # ---- Pass 2: synthesize annotations and decorate each type in place. ----
    gql_types: dict[type, type] = {}
    for orm_class in orm_classes:
        cls = bare_types[orm_class]
        annotations: dict[str, Any] = {}
        for column in orm_class.__table__.columns:
            annotations[column.name] = _column_annotation(column)
        for rel_name, rel in orm_class.__mapper__.relationships.items():
            target = bare_types.get(rel.mapper.class_)
            if target is None:
                continue
            annotations[rel_name] = list[target] if rel.uselist else target | None
        cls.__annotations__ = annotations
        gql_types[orm_class] = orm.type(
            orm_class,
            name=_gql_type_name(orm_class),
            filters=cls.__orm_filter__,
            order=cls.__orm_order__,
        )(cls)

    # ---- Query root ----
    class Query:
        """Root GraphQL query type for this build."""

        @strawberry.field
        def current_user(self, info: strawberry.Info[CustomHTTPContextType, None]) -> JWTUser | None:
            return info.context.request.user or None

    Query.__annotations__ = {}
    for orm_class in orm_classes:
        table_name = orm_class.__table__.name
        gql_type = gql_types[orm_class]

        # List field: orm.field() builds a list resolver with filter/order args.
        list_field = orm.field(description=f"List {table_name} with filtering and ordering.")
        Query.__annotations__[table_name] = list[gql_type]
        setattr(Query, table_name, list_field)
        # ``orm.field()`` is a descriptor that configures itself in
        # ``__set_name__`` — trigger it manually since we assign post-hoc.
        list_field.__set_name__(Query, table_name)

        # Primary-key lookup field.
        _attach_pk_field(Query, orm_class, gql_type)

    schema = orm.schema(
        query=strawberry.type(Query),
        extensions=[QueryDepthLimiter(max_depth=_MAX_WHERE_DEPTH)],
    )
    return make_graphql_controller(
        schema,
        path=f"{settings.base_path}/graphql",
        context_getter=custom_context_getter,
        allow_queries_via_get=False,
        graphql_ide="graphiql" if settings.ui_enabled else None,
        keep_alive=True,
    )


def _attach_pk_field(query_cls: type, orm_class: type, gql_type: type) -> None:
    """Attach a primary-key lookup field returning a single record to ``query_cls``."""
    table = orm_class.__table__
    pks = list(table.primary_key.columns.keys())
    pk_field_name = inflect.singular_noun(table.name) or table.name

    async def pk_resolver(info: strawberry.Info[CustomHTTPContextType, None], **kwids: object) -> gql_type:  # type: ignore[valid-type]
        statement = select(orm_class)
        for key, value in kwids.items():
            statement = statement.where(getattr(orm_class, key) == value)
        session = info.context.session
        result = (await session.execute(statement)).scalar_one_or_none()
        if result is None:
            raise RecordNotFoundError(f"No {table.name} record matches {kwids}")
        return result

    field = strawberry.field(resolver=pk_resolver, description=f"Get a {pk_field_name} by primary key.")
    setattr(query_cls, pk_field_name, field)
    query_cls.__annotations__[pk_field_name] = gql_type
    _set_resolver_arguments(
        field,
        [
            StrawberryArgument(
                python_name=pk,
                graphql_name=None,
                type_annotation=StrawberryAnnotation(table.primary_key.columns[pk].type.python_type),
            )
            for pk in pks
        ],
    )
