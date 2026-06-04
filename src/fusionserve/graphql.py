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
from sqlalchemy import delete, event, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from strawberry import relay
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
from strawberry.utils.str_converters import to_camel_case
from strawberry_orm import StrawberryORM
from strawberry_orm.relay import ORMListConnection

from . import auth
from .config import settings
from .models import FunctionInfo, FunctionReturnKind, Introspection, RecordNotFoundError, SmartComment
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

    The ``_orm_*`` fields are slots the strawberry-orm filter/order extension
    and relay connection machinery stash query state on. ``BaseContext`` is a
    slotted ``msgspec.Struct``, so they must be declared here for the writes to
    succeed (the reads use ``getattr(..., None)``).
    """

    session: AsyncSession
    _orm_base_query: Any = None
    _orm_backend: Any = None
    _orm_group_by: Any = None
    _orm_order: Any = None


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


#: Private strawberry-orm backend attributes the dynamic builder depends on.
#: Driving the library dynamically has no public accessor for the per-model
#: filter/order type registries in 0.13.0, so we read them directly — guarded
#: by :func:`_orm_registry` and pinned by a unit test so a breaking upgrade
#: fails loudly instead of silently dropping filter/order args.
_FILTER_REGISTRY_ATTR = "_filter_registry"
_ORDER_REGISTRY_ATTR = "_order_registry"


def _orm_registry(backend: object, attr: str) -> dict:
    """Return a strawberry-orm backend registry, or raise a clear upgrade error."""
    registry = getattr(backend, attr, None)
    if registry is None:
        raise RuntimeError(
            f"strawberry-orm backend is missing the private {attr!r} registry this build relies on. "
            f"A strawberry-orm upgrade likely changed its internals — update fusionserve.graphql.build()."
        )
    return registry


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


class _UnmappableFunctionError(RuntimeError):
    """Signal that a :class:`FunctionInfo` can't be wired into the schema.

    Caught in :func:`build` so the offending function is logged and skipped
    rather than aborting startup.
    """


def _function_return_annotation(fn: FunctionInfo, gql_types_by_table: dict[str, type]):
    """Resolve the GraphQL return annotation for a custom-query function.

    Returns ``T | None`` for ``SCALAR``, ``GqlType | None`` for ``ROW``, and
    ``list[GqlType]`` for ``SET``.

    Raises:
        _UnmappableFunctionError: If a ROW/SET return table has no mapped type.
    """
    if fn.return_kind == FunctionReturnKind.SCALAR:
        return fn.return_python_type | None
    gql_type = gql_types_by_table.get(fn.return_table_name)
    if gql_type is None:
        raise _UnmappableFunctionError(f"return table {fn.return_table_name!r} has no GraphQL type mapped")
    if fn.return_kind == FunctionReturnKind.SET:
        return list[gql_type]
    return gql_type | None


def _build_function_resolver(fn: FunctionInfo, base):
    """Build the async resolver for a custom-query PostgreSQL function.

    Runs on the request's role-scoped context session (so RLS applies) using
    PostgreSQL named-argument call syntax (``arg := :arg``) so UNSET arguments
    fall back to the function's declared defaults. ROW/SET returns are read as
    automap ORM instances via ``select(orm_class).from_statement(...)``.
    """
    qualified = f'"{fn.schema}"."{fn.name}"'

    async def resolver(info: strawberry.Info[CustomHTTPContextType, None], **kwargs: object):
        bind = {k: v for k, v in kwargs.items() if v is not strawberry.UNSET}
        placeholders = ", ".join(f"{name} := :{name}" for name in bind)
        session = info.context.session
        if fn.return_kind == FunctionReturnKind.SCALAR:
            sql = f"SELECT {qualified}({placeholders}) AS value"
            return (await session.execute(text(sql).bindparams(**bind))).scalar()
        sql = f"SELECT * FROM {qualified}({placeholders})"
        orm_class = base.classes[fn.return_table_name]
        statement = select(orm_class).from_statement(text(sql).bindparams(**bind))
        rows = (await session.execute(statement)).scalars().all()
        if fn.return_kind == FunctionReturnKind.SET:
            return list(rows)
        return rows[0] if rows else None

    return resolver


def _attach_function_fields(query_cls: type, introspection: Introspection, gql_types: dict[type, type]) -> None:
    """Attach one Query field per STABLE/IMMUTABLE PostgreSQL function.

    Functions whose name collides with an existing query field, or whose
    ROW/SET return table has no mapped GraphQL type, are logged and skipped.
    """
    gql_types_by_table = {orm_class.__table__.name: gql for orm_class, gql in gql_types.items()}
    existing = {name for name in dir(query_cls) if not name.startswith("__")}
    for fn in introspection.functions:
        field_name = to_camel_case(fn.name)
        if field_name in existing:
            _logger.warning(
                "Skipping GraphQL exposure of function %s.%s: field name %r collides with an existing field.",
                fn.schema,
                fn.name,
                field_name,
            )
            continue
        try:
            return_annotation = _function_return_annotation(fn, gql_types_by_table)
        except _UnmappableFunctionError as exc:
            _logger.warning("Skipping GraphQL exposure of function %s.%s: %s", fn.schema, fn.name, exc)
            continue
        field = strawberry.field(
            resolver=_build_function_resolver(fn, introspection.base),
            description=fn.description or f"Custom query exposing {fn.schema}.{fn.name}().",
        )
        field.type_annotation = StrawberryAnnotation(return_annotation)
        setattr(query_cls, field_name, field)
        arguments: list[StrawberryArgument] = []
        for p in fn.params:
            annotation = StrawberryAnnotation(p.python_type | None if p.has_default else p.python_type)
            if p.has_default:
                arguments.append(StrawberryArgument(p.name, p.name, annotation, default=strawberry.UNSET))
            else:
                arguments.append(StrawberryArgument(p.name, p.name, annotation))
        _set_resolver_arguments(field, arguments)
        existing.add(field_name)


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
    filter_registry = _orm_registry(orm.backend, _FILTER_REGISTRY_ATTR)
    order_registry = _orm_registry(orm.backend, _ORDER_REGISTRY_ATTR)

    generated_module = _types_mod.ModuleType(_GENERATED_MODULE)
    sys.modules[_GENERATED_MODULE] = generated_module
    bare_types: dict[type, type] = {}
    # Tables with a single-column PK are exposed as relay nodes (and their
    # top-level query as a relay connection); composite-PK tables can't carry a
    # single ``relay.NodeID`` so they fall back to a plain list field.
    single_pk: dict[type, bool] = {}
    for orm_class in orm_classes:
        orm.filter(orm_class)
        orm.order(orm_class)
        name = _gql_type_name(orm_class)
        is_single_pk = len(orm_class.__table__.primary_key.columns) == 1
        single_pk[orm_class] = is_single_pk
        bases = (relay.Node,) if is_single_pk else ()
        cls = type(name, bases, {})
        cls.__module__ = _GENERATED_MODULE
        cls.__orm_model__ = orm_class
        cls.__orm_filter__ = filter_registry.get(orm_class)
        cls.__orm_order__ = order_registry.get(orm_class)
        setattr(generated_module, name, cls)
        bare_types[orm_class] = cls

    # ---- Loop B: decorate each type in place and attach its root fields. ----
    # All bare classes exist now, so relationship annotations resolve and each
    # type's Query/Mutation fields need only that type's own decorated ``gql_type``.
    gql_types: dict[type, type] = {}
    has_mutations = False
    for orm_class in orm_classes:
        cls = bare_types[orm_class]
        table = orm_class.__table__
        table_name = table.name
        pk_column = table.primary_key.columns[0] if single_pk[orm_class] else None
        annotations: dict[str, Any] = {}
        for column in table.columns:
            if column is pk_column:
                # Mark the single PK column as the relay node id.
                annotations[column.name] = relay.NodeID[column.type.python_type]  # type: ignore[misc]
            else:
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
        _apply_descriptions(gql_type, table)
        gql_types[orm_class] = gql_type

        # ---- Query: list/connection field. ----
        if single_pk[orm_class]:
            # Relay connection (edges/pageInfo/totalCount + filter/order args).
            conn_field = orm.connection()
            Query.__annotations__[table_name] = ORMListConnection[gql_type]
            setattr(Query, table_name, conn_field)
            conn_field.__set_name__(Query, table_name)
        else:
            # Composite-PK fallback: plain list field.
            list_field = orm.field(description=f"List {table_name} with filtering and ordering.")
            Query.__annotations__[table_name] = list[gql_type]
            setattr(Query, table_name, list_field)
            list_field.__set_name__(Query, table_name)

        # ---- Query: primary-key lookup field. ----
        _attach_pk_field(Query, orm_class, gql_type)

        # ---- Mutations (views are read-only). ----
        if table_name not in introspection.views:
            _attach_mutations(Mutation, orm, orm_class, gql_type)
            has_mutations = True

    # ---- Custom queries from STABLE/IMMUTABLE PostgreSQL functions. ----
    _attach_function_fields(Query, introspection, gql_types)

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
