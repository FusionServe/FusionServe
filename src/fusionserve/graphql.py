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
from sqlalchemy import delete, event, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from strawberry.annotation import StrawberryAnnotation
from strawberry.extensions import QueryDepthLimiter
from strawberry.litestar import (
    BaseContext,
    HTTPContextType,
    WebSocketContextType,
    make_graphql_controller,
)
from strawberry.scalars import JSON as StrawberryJSON
from strawberry.types.arguments import StrawberryArgument
from strawberry_orm import StrawberryORM

from . import auth
from .config import settings
from .models import Introspection, RecordNotFoundError, SmartComment
from .persistence import async_session, inflect, role_config_statement, set_role

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


def _reapply_role_on_begin(session, transaction, connection) -> None:
    """``after_begin`` hook: re-apply the request's role to each new transaction.

    The role set by :func:`fusionserve.persistence.set_role` is transaction-local
    (``is_local=True``), so it is discarded when a transaction ends — including
    the implicit commit a mutation performs. Mutations then do post-commit work
    (``refresh``, and nested mutation-payload field resolution) in a *new*
    transaction, which would otherwise run under the wrong role and bypass RLS.

    Registering this on the session's sync session re-issues the role
    configuration synchronously on the connection backing every transaction
    (initial and post-commit alike). The authenticated user is read from
    ``session.info`` where :func:`custom_context_getter` stashes it.
    """
    user = session.info.get("fs_user")
    connection.execute(role_config_statement(user))


async def custom_context_getter(request: litestar.Request) -> AsyncGenerator[CustomContext]:
    """Open one role-scoped async session per GraphQL request.

    A single :class:`~sqlalchemy.ext.asyncio.AsyncSession` is opened and the
    request user is stashed on ``session.info`` so the ``after_begin`` hook
    (:func:`_reapply_role_on_begin`) applies the user's PostgreSQL role to
    **every** transaction the session opens during the request — the initial
    one and any post-commit transaction created by mutations. The session is
    exposed on the context so the strawberry-orm backend's ``session_getter``
    reuses it for the whole request, then closed when the request completes.

    The role configuration is transaction-local, so the pooled connection never
    carries a role back to the pool. Strawberry's Litestar integration wires
    this via ``litestar.di.Provide``, which honours async-generator
    dependencies, giving correct per-request session lifecycle.

    Args:
        request: The incoming Litestar HTTP request.

    Yields:
        A :class:`CustomContext` wrapping the role-scoped session.
    """
    session = async_session()
    session.info["fs_user"] = request.user
    event.listen(session.sync_session, "after_begin", _reapply_role_on_begin)
    # Apply immediately so the role is set even if the first statement does not
    # implicitly begin via the ORM event (defensive; after_begin also covers it).
    await set_role(session, request.user)
    try:
        yield CustomContext(session=session)
    finally:
        await session.close()


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
    it in ``| None`` when the column is nullable.

    JSON / JSONB columns report ``python_type`` as ``dict`` (or ``list``), which
    is not a valid GraphQL output type, so they are mapped to Strawberry's
    ``JSON`` scalar. Columns whose type has no Python equivalent fall back to
    :data:`strawberry_orm.auto` (the backend maps it to ``str``).
    """
    try:
        python_type = column.type.python_type
    except NotImplementedError:
        from strawberry_orm import auto

        return auto
    if python_type in (dict, list):
        python_type = StrawberryJSON
    return python_type | None if column.nullable else python_type


def _set_resolver_arguments(field, arguments: list[StrawberryArgument]) -> None:
    """Replace a Strawberry field's resolver argument list in place."""
    field.base_resolver.arguments = arguments


def _apply_descriptions(gql_type: type, table: Any) -> None:
    """Copy smart-comment text onto the type and its column field descriptions.

    ``orm.type()`` exposes no description hook, so descriptions are applied
    after decoration by mutating the produced Strawberry definition. The table
    comment becomes the type description; each column comment becomes the
    matching field's description. Relationship/non-column fields are untouched.
    """
    table_comment = SmartComment.from_object(table).content
    if table_comment:
        gql_type.__strawberry_definition__.description = table_comment
    for column in table.columns:
        field = gql_type.__strawberry_definition__.get_field(column.name)
        if field is None:
            continue
        content = SmartComment.from_object(column).content
        if content:
            field.description = content


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
        session_getter=lambda info: info.context.session,
        default_query_limit=settings.default_page_size,
        max_filter_depth=_MAX_WHERE_DEPTH,
    )

    class Query:
        """Root GraphQL query type for this build."""

        @strawberry.field
        def current_user(self, info: strawberry.Info[CustomHTTPContextType, None]) -> JWTUser | None:
            return info.context.request.user or None

    class Mutation:
        """Root GraphQL mutation type for this build."""

    Query.__annotations__ = {}
    Mutation.__annotations__ = {}

    # Iterate in a stable, name-sorted order. ``automap``'s class collection has
    # no guaranteed iteration order across processes, and the order determines
    # which side of a bidirectional relationship gets its ``object`` filter wired
    # (see the spec's friction log). Sorting keeps the generated schema shape
    # deterministic across restarts/deploys.
    orm_classes = sorted(introspection.base.classes, key=lambda c: c.__table__.name)

    # ---- Loop A: register filter/order types and pre-create bare type classes. ----
    # The backend keys filter/order types in per-instance registries; cyclic
    # relationships (e.g. author.books / book.author) mean a type being decorated
    # references another not-yet-decorated type. Registering filters/orders and
    # creating *all* bare classes (with their orm metadata pre-seeded) before any
    # decoration lets the backend's relation wiring resolve every reference;
    # ``strawberry.type`` later decorates each bare class in place, so annotations
    # already point at the final types. Each iteration only reads its own registry
    # entry, so iteration order is irrelevant here.
    generated_module = _types_mod.ModuleType(_GENERATED_MODULE)
    sys.modules[_GENERATED_MODULE] = generated_module
    bare_types: dict[type, type] = {}
    for orm_class in orm_classes:
        orm.filter(orm_class)
        orm.order(orm_class)
        name = _gql_type_name(orm_class)
        cls = type(name, (), {})
        cls.__module__ = _GENERATED_MODULE
        cls.__orm_model__ = orm_class
        cls.__orm_filter__ = orm.backend._filter_registry.get(orm_class)
        cls.__orm_order__ = orm.backend._order_registry.get(orm_class)
        setattr(generated_module, name, cls)
        bare_types[orm_class] = cls

    # ---- Loop B: decorate each type in place and attach its root fields. ----
    # All bare classes exist now, so relationship annotations resolve and each
    # type's Query/Mutation fields need only that type's own decorated ``gql_type``.
    gql_types: dict[type, type] = {}
    has_mutations = False
    for orm_class in orm_classes:
        cls = bare_types[orm_class]
        table_name = orm_class.__table__.name
        annotations: dict[str, Any] = {}
        for column in orm_class.__table__.columns:
            annotations[column.name] = _column_annotation(column)
        for rel_name, rel in orm_class.__mapper__.relationships.items():
            target = bare_types.get(rel.mapper.class_)
            if target is None:
                continue
            annotations[rel_name] = list[target] if rel.uselist else target | None
        cls.__annotations__ = annotations
        gql_type = orm.type(
            orm_class,
            name=_gql_type_name(orm_class),
            filters=cls.__orm_filter__,
            order=cls.__orm_order__,
        )(cls)
        _apply_descriptions(gql_type, orm_class.__table__)
        gql_types[orm_class] = gql_type

        # ---- Query: list field (orm.field() builds the list resolver). ----
        list_field = orm.field(description=f"List {table_name} with filtering and ordering.")
        Query.__annotations__[table_name] = list[gql_type]
        setattr(Query, table_name, list_field)
        # ``orm.field()`` is a descriptor that configures itself in
        # ``__set_name__`` — trigger it manually since we assign post-hoc.
        list_field.__set_name__(Query, table_name)

        # ---- Query: primary-key lookup field. ----
        _attach_pk_field(Query, orm_class, gql_type)

        # ---- Mutations (views are read-only). ----
        if table_name not in introspection.views:
            _attach_mutations(Mutation, orm, orm_class, gql_type)
            has_mutations = True

    schema = orm.schema(
        query=strawberry.type(Query),
        mutation=strawberry.type(Mutation) if has_mutations else None,
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
    field.type_annotation = StrawberryAnnotation(gql_type)
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


def _set_fields(values: object) -> dict[str, Any]:
    """Return the explicitly-set (non-UNSET) fields of an input/partial instance."""
    return {k: v for k, v in vars(values).items() if v is not strawberry.UNSET}


def _pk_argument(table, pk: str) -> StrawberryArgument:
    """Build a StrawberryArgument for a primary-key column."""
    return StrawberryArgument(
        python_name=pk,
        graphql_name=None,
        type_annotation=StrawberryAnnotation(table.primary_key.columns[pk].type.python_type),
    )


def _attach_mutations(mutation_cls: type, orm: StrawberryORM, orm_class: type, gql_type: type) -> None:
    """Attach the six CRUD mutations for a table to ``mutation_cls``.

    Mirrors the previous implementation's field names and RETURNING-based
    single-roundtrip semantics, but derives input/patch types from
    ``orm.input`` / ``orm.partial`` and filter inputs from ``orm.filter``. The
    empty-``where`` guardrail on ``updateMany`` / ``deleteMany`` is preserved.
    """
    table = orm_class.__table__
    pks = list(table.primary_key.columns.keys())
    singular = inflect.singular_noun(table.name) or table.name
    input_type = orm.input(orm_class)
    patch_type = orm.partial(orm_class)
    where_type = orm.backend._filter_registry.get(orm_class)

    def _condition_from_where(where: object):
        stmt = orm.backend.apply_filters(select(orm_class), where, orm_class)
        return stmt.whereclause

    async def create_resolver(info: strawberry.Info, input: object) -> gql_type:  # type: ignore[valid-type]
        session = info.context.session
        instance = orm_class(**_set_fields(input))
        session.add(instance)
        await session.commit()
        await session.refresh(instance)
        return instance

    async def create_many_resolver(info: strawberry.Info, inputs: Any) -> list[gql_type]:  # type: ignore[valid-type]
        if not inputs:
            raise ValueError("inputs must contain at least one record to create")
        session = info.context.session
        rows = [_set_fields(item) for item in inputs]
        statement = insert(orm_class).values(rows).returning(orm_class)
        result = (await session.execute(statement)).scalars().all()
        await session.commit()
        return list(result)

    async def update_resolver(info: strawberry.Info, patch: object, **kwids: object) -> gql_type:  # type: ignore[valid-type]
        session = info.context.session
        statement = select(orm_class)
        for key, value in kwids.items():
            statement = statement.where(getattr(orm_class, key) == value)
        result = (await session.execute(statement)).scalar_one_or_none()
        if result is None:
            raise RecordNotFoundError(f"No {table.name} record matches {kwids}")
        for key, value in _set_fields(patch).items():
            setattr(result, key, value)
        await session.commit()
        await session.refresh(result)
        return result

    async def update_many_resolver(info: strawberry.Info, patch: object, where: object) -> list[gql_type]:  # type: ignore[valid-type]
        values = _set_fields(patch)
        if not values:
            raise ValueError("patch must contain at least one field to update")
        condition = _condition_from_where(where)
        if condition is None:
            raise ValueError("where must contain at least one filter condition for update_many")
        session = info.context.session
        statement = (
            update(orm_class)
            .where(condition)
            .values(**values)
            .returning(orm_class)
            .execution_options(synchronize_session=None)
        )
        rows = (await session.execute(statement)).scalars().all()
        await session.commit()
        return list(rows)

    async def delete_resolver(info: strawberry.Info, **kwids: object) -> gql_type:  # type: ignore[valid-type]
        session = info.context.session
        statement = (
            delete(orm_class)
            .where(*[getattr(orm_class, key) == value for key, value in kwids.items()])
            .returning(orm_class)
            .execution_options(synchronize_session=None)
        )
        result = (await session.execute(statement)).scalar_one_or_none()
        if result is None:
            raise RecordNotFoundError(f"No {table.name} record matches {kwids}")
        await session.commit()
        return result

    async def delete_many_resolver(info: strawberry.Info, where: object) -> list[gql_type]:  # type: ignore[valid-type]
        condition = _condition_from_where(where)
        if condition is None:
            raise ValueError("where must contain at least one filter condition for delete_many")
        session = info.context.session
        statement = delete(orm_class).where(condition).returning(orm_class).execution_options(synchronize_session=None)
        rows = (await session.execute(statement)).scalars().all()
        await session.commit()
        return list(rows)

    # ``from __future__ import annotations`` stringifies the resolver return
    # hints, which strawberry cannot resolve (``gql_type`` is a local), so set
    # each field's return type explicitly.
    single = StrawberryAnnotation(gql_type)
    many = StrawberryAnnotation(list[gql_type])

    create_one = strawberry.mutation(resolver=create_resolver, description=f"Create a new {singular}.")
    create_one.type_annotation = single
    setattr(mutation_cls, f"create{to_pascal(singular)}", create_one)
    _set_resolver_arguments(
        create_one,
        [StrawberryArgument("input", None, StrawberryAnnotation(input_type))],
    )

    create_many = strawberry.mutation(resolver=create_many_resolver, description=f"Create many {table.name}.")
    create_many.type_annotation = many
    setattr(mutation_cls, f"create{to_pascal(table.name)}", create_many)
    _set_resolver_arguments(
        create_many,
        [StrawberryArgument("inputs", None, StrawberryAnnotation(list[input_type]))],
    )

    update_one = strawberry.mutation(resolver=update_resolver, description=f"Update a {singular} by primary key.")
    update_one.type_annotation = single
    setattr(mutation_cls, f"update{to_pascal(singular)}", update_one)
    _set_resolver_arguments(
        update_one,
        [StrawberryArgument("patch", None, StrawberryAnnotation(patch_type)), *[_pk_argument(table, pk) for pk in pks]],
    )

    update_many = strawberry.mutation(resolver=update_many_resolver, description=f"Update many {table.name}.")
    update_many.type_annotation = many
    setattr(mutation_cls, f"update{to_pascal(table.name)}", update_many)
    _set_resolver_arguments(
        update_many,
        [
            StrawberryArgument("patch", None, StrawberryAnnotation(patch_type)),
            StrawberryArgument("where", None, StrawberryAnnotation(where_type)),
        ],
    )

    delete_one = strawberry.mutation(resolver=delete_resolver, description=f"Delete a {singular} by primary key.")
    delete_one.type_annotation = single
    setattr(mutation_cls, f"delete{to_pascal(singular)}", delete_one)
    _set_resolver_arguments(delete_one, [_pk_argument(table, pk) for pk in pks])

    delete_many = strawberry.mutation(resolver=delete_many_resolver, description=f"Delete many {table.name}.")
    delete_many.type_annotation = many
    setattr(mutation_cls, f"delete{to_pascal(table.name)}", delete_many)
    _set_resolver_arguments(
        delete_many,
        [StrawberryArgument("where", None, StrawberryAnnotation(where_type))],
    )
