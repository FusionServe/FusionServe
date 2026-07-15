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
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import litestar.datastructures
import strawberry
from litestar import Request
from pydantic.alias_generators import to_pascal
from sqlalchemy import delete, event, insert, select, text, tuple_, update
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
from strawberry.utils.str_converters import to_camel_case
from strawberry_orm import StrawberryORM

from . import auth
from .config import settings
from .connections import build_connection_field, materialize
from .models import (
    FILTER_OVERRIDES,
    FunctionInfo,
    FunctionReturnKind,
    Introspection,
    RecordNotFoundError,
    SmartComment,
)
from .persistence import async_session, inflect, role_config_statement

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

    request: Request[auth.User, Any, litestar.datastructures.State]


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
    role = settings.anonymous_role if not user else user.role
    _logger.debug("Setting role to %s", role)
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
    # The after_begin hook applies the role to every transaction the session
    # opens (initial + post-commit), so no explicit set_role is needed here.
    event.listen(session.sync_session, "after_begin", _reapply_role_on_begin)
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


def _patch_strawberry_orm_uuid() -> None:
    """Make strawberry-orm's SQLAlchemy backend treat PostgreSQL ``uuid`` as UUID.

    The backend hard-codes ``uuid`` columns to ``str`` in two places that its
    public ``filter_overrides`` hook cannot reach, both of which break uuid
    filtering under the asyncpg dialect (``uuid = $1::VARCHAR`` →
    ``operator does not exist: uuid = character varying``):

    * ``_SA_TYPE_MAP["UUID"]`` maps ``uuid`` columns to ``str`` during
      introspection, so non-PK uuid columns never resolve to a uuid-typed
      lookup. Repointing it at :class:`uuid.UUID` lets
      :data:`fusionserve.models.FILTER_OVERRIDES` supply the uuid lookup (and,
      as a bonus, types uuid mutation-input fields as the UUID scalar — matching
      the already-uuid output type).
    * ``_SA_TYPE_MAP["JSON"]`` maps ``json`` columns to ``str`` and ``jsonb``
      columns fall through to the same ``str`` default (``"JSONB"`` is absent),
      so JSON/JSONB mutation-input fields are typed ``String`` — forcing clients
      to send a JSON-encoded string instead of a JSON value, and mismatching the
      ``JSON``-scalar output type produced by :func:`_column_annotation`.
      Repointing both ``"JSON"`` and ``"JSONB"`` at Strawberry's ``JSON`` scalar
      types the input fields as ``JSON``, symmetric with reads.
    * ``_coerce_reference_value`` powers ``ReferenceLookup`` (used for every
      non-int primary key and uuid FK), coercing values to ``int`` or ``str``
      only — never ``uuid.UUID``. Wrapping it to try ``int`` → ``uuid.UUID`` →
      ``str`` fixes the common ``id: {exact: …}`` / ``object.<rel>`` uuid case
      while staying backward compatible (integer ids still parse first; plain
      string keys still fall through).

    Both mutations are idempotent and guarded by ``tests/test_graphql_orm_contract.py``
    so a strawberry-orm upgrade that changes these internals fails loudly.

    Raises:
        RuntimeError: If either relied-upon symbol is missing from the backend
            module (signals a breaking strawberry-orm upgrade).
    """
    from strawberry_orm.backends import sqlalchemy as _sa_backend

    type_map = getattr(_sa_backend, "_SA_TYPE_MAP", None)
    if not isinstance(type_map, dict) or "UUID" not in type_map or "JSON" not in type_map:
        raise RuntimeError(
            "strawberry-orm SQLAlchemy backend is missing the private '_SA_TYPE_MAP' "
            "this build relies on. A strawberry-orm upgrade likely changed its internals "
            "— update fusionserve.graphql._patch_strawberry_orm_uuid()."
        )
    type_map["UUID"] = uuid.UUID
    type_map["JSON"] = StrawberryJSON
    type_map["JSONB"] = StrawberryJSON

    original = getattr(_sa_backend, "_coerce_reference_value", None)
    if original is None:
        raise RuntimeError(
            "strawberry-orm SQLAlchemy backend is missing the private "
            "'_coerce_reference_value' this build relies on. A strawberry-orm upgrade "
            "likely changed its internals — update "
            "fusionserve.graphql._patch_strawberry_orm_uuid()."
        )
    if getattr(original, "_fusionserve_uuid_patched", False):
        return

    def _coerce_reference_value(val: Any) -> Any:
        if isinstance(val, list):
            return [_coerce_reference_value(item) for item in val]
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            return val
        text = str(val)
        try:
            return int(text)
        except ValueError:
            pass
        try:
            return uuid.UUID(text)
        except ValueError:
            return text

    _coerce_reference_value._fusionserve_uuid_patched = True  # type: ignore[attr-defined]
    _sa_backend._coerce_reference_value = _coerce_reference_value


_patch_strawberry_orm_uuid()


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
        filter_overrides=FILTER_OVERRIDES,
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

    # Iterate in foreign-key dependency order (``MetaData.sorted_tables`` yields
    # referenced tables before the tables that reference them). This is
    # deterministic per schema (``automap``'s class collection has no guaranteed
    # iteration order across processes) and registers "leaf" models first, so a
    # bidirectional relationship's ``object`` filter is wired on the referencing
    # side (e.g. ``booksFilter.object.author``); ``sorted_tables`` still returns a
    # total order for FK cycles. ``sorted_tables`` includes reflected tables with
    # no mapped class (unmapped views, automap-collapsed association tables), so
    # skip any name absent from ``base.classes``.
    orm_classes = [
        introspection.base.classes[table.name]
        for table in introspection.base.metadata.sorted_tables
        if table.name in introspection.base.classes
    ]

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
    for orm_class in orm_classes:
        orm.filter(orm_class)
        orm.order(orm_class)
        name = _gql_type_name(orm_class)
        cls = type(name, (), {})
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
        annotations: dict[str, Any] = {}
        for column in table.columns:
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

        # ---- Query: connection field (cursor + limit/offset pagination). ----
        conn_field = build_connection_field(
            orm,
            orm_class,
            gql_type,
            cls.__orm_filter__,
            cls.__orm_order__,
            _gql_type_name(orm_class),
            description=f"List {table_name} with filtering, ordering and pagination.",
        )
        setattr(Query, table_name, conn_field)

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
        # Route through the optimizer so nested relations selected on the record
        # are eager-loaded (otherwise they async-lazy-load -> greenlet error).
        rows = await materialize(statement, info)
        result = rows[0] if rows else None
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
    # Pure join tables (every column is part of the PK) have no non-PK columns:
    # the create input must then *include* the PK columns (otherwise it's empty
    # and GraphQL rejects it), and there is nothing to patch, so update
    # mutations are skipped.
    # TODO: this never fires since automap ignores pure join tables.
    has_non_pk = any(col.name not in pks for col in table.columns)
    input_type = orm.input(orm_class) if has_non_pk else orm.input(orm_class, exclude_pk=False)
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
        values = _set_fields(patch)
        if not values:
            raise ValueError("patch must contain at least one field to update")
        session = info.context.session
        statement = (
            update(orm_class)
            .where(*[getattr(orm_class, key) == value for key, value in kwids.items()])
            .values(**values)
            .returning(orm_class)
            .execution_options(synchronize_session=None)
        )
        result = (await session.execute(statement)).scalar_one_or_none()
        if result is None:
            raise RecordNotFoundError(f"No {table.name} record matches {kwids}")
        await session.commit()
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

    # Update mutations only exist when there are non-PK columns to patch.
    if has_non_pk:
        patch_type = orm.partial(orm_class)
        update_one = strawberry.mutation(resolver=update_resolver, description=f"Update a {singular} by primary key.")
        update_one.type_annotation = single
        setattr(mutation_cls, f"update{to_pascal(singular)}", update_one)
        _set_resolver_arguments(
            update_one,
            [
                StrawberryArgument("patch", None, StrawberryAnnotation(patch_type)),
                *[_pk_argument(table, pk) for pk in pks],
            ],
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

    # ---- Many-to-many link/unlink mutations (one set per association side). ----
    for rel in orm_class.__mapper__.relationships:
        if rel.secondary is not None:
            _attach_m2m_mutations(mutation_cls, orm_class, gql_type, rel)


def _split_assoc_pair(pair, secondary) -> tuple:
    """Split a relationship sync pair into ``(entity_column, secondary_column)``.

    The pair members come in an unspecified order; classify them by which one
    belongs to the association (``secondary``) table.
    """
    left, right = pair
    return (left, right) if right.table is secondary else (right, left)


def _attach_m2m_mutations(mutation_cls: type, orm_class: type, gql_type: type, rel) -> None:
    """Attach plural link/unlink mutations for one side of a many-to-many relation.

    automap skips the pure association table, exposing only ``secondary``-based
    relationships. For each side we generate two mutations (no singular, no
    update) operating directly on the association table:

    * ``create<LocalSingular><TargetPlural>(inputs: [<Local><Target>Link!]!)``
    * ``delete<LocalSingular><TargetPlural>(inputs: [<Local><Target>Link!]!)``

    Each runs a single statement (``INSERT``/``DELETE … RETURNING`` wrapped in a
    CTE joined back to the local table) so the role-scoped session returns the
    affected **local** entities without an extra round-trip. Semantics are
    strict: linking an existing pair errors (PK violation, atomic); unlinking is
    rejected unless every requested pair existed.
    """
    secondary = rel.secondary
    target_orm = rel.mapper.class_
    # Restrict to single-column joins on each side (the normal M2M shape).
    if len(rel.synchronize_pairs) != 1 or len(rel.secondary_synchronize_pairs) != 1:
        _logger.warning(
            "Skipping M2M mutations for %s.%s: multi-column association joins are unsupported.",
            orm_class.__table__.name,
            rel.key,
        )
        return

    local_pk_col, sec_local_col = _split_assoc_pair(rel.synchronize_pairs[0], secondary)
    _target_pk_col, sec_target_col = _split_assoc_pair(rel.secondary_synchronize_pairs[0], secondary)
    local_pk_attr = getattr(orm_class, local_pk_col.name)

    local_singular = _gql_type_name(orm_class)
    target_plural = to_pascal(target_orm.__table__.name)
    link_name = f"{local_singular}{_gql_type_name(target_orm)}Link"

    link_input = strawberry.input(
        type(
            link_name,
            (),
            {
                "__annotations__": {
                    sec_local_col.name: sec_local_col.type.python_type,
                    sec_target_col.name: sec_target_col.type.python_type,
                }
            },
        )
    )

    def _rows(inputs: list) -> list[dict]:
        return [
            {
                sec_local_col.name: getattr(item, sec_local_col.name),
                sec_target_col.name: getattr(item, sec_target_col.name),
            }
            for item in inputs
        ]

    def _locals_from_cte(cte) -> Any:
        return select(orm_class).join(cte, local_pk_attr == cte.c[sec_local_col.name])

    def _dedupe(rows: list) -> list:
        seen: dict[Any, Any] = {}
        for row in rows:
            seen.setdefault(getattr(row, local_pk_col.name), row)
        return list(seen.values())

    async def create_resolver(info: strawberry.Info, inputs: Any) -> list[gql_type]:  # type: ignore[valid-type]
        if not inputs:
            raise ValueError("inputs must contain at least one link to create")
        session = info.context.session
        cte = insert(secondary).values(_rows(inputs)).returning(sec_local_col).cte("linked")
        rows = (await session.execute(_locals_from_cte(cte))).scalars().all()
        await session.commit()
        return _dedupe(list(rows))

    async def delete_resolver(info: strawberry.Info, inputs: Any) -> list[gql_type]:  # type: ignore[valid-type]
        if not inputs:
            raise ValueError("inputs must contain at least one link to delete")
        pairs = [(getattr(i, sec_local_col.name), getattr(i, sec_target_col.name)) for i in inputs]
        session = info.context.session
        cte = (
            delete(secondary)
            .where(tuple_(sec_local_col, sec_target_col).in_(pairs))
            .returning(sec_local_col)
            .execution_options(synchronize_session=None)
            .cte("unlinked")
        )
        rows = list((await session.execute(_locals_from_cte(cte))).scalars().all())
        if len(rows) != len(pairs):
            await session.rollback()
            raise RecordNotFoundError(f"one or more {link_name} pairs do not exist")
        await session.commit()
        return _dedupe(rows)

    many_local = StrawberryAnnotation(list[gql_type])
    create_links = strawberry.mutation(
        resolver=create_resolver, description=f"Link {local_singular} to {target_plural} (many-to-many)."
    )
    create_links.type_annotation = many_local
    setattr(mutation_cls, f"create{local_singular}{target_plural}", create_links)
    _set_resolver_arguments(create_links, [StrawberryArgument("inputs", None, StrawberryAnnotation(list[link_input]))])

    delete_links = strawberry.mutation(
        resolver=delete_resolver, description=f"Unlink {local_singular} from {target_plural} (many-to-many)."
    )
    delete_links.type_annotation = many_local
    setattr(mutation_cls, f"delete{local_singular}{target_plural}", delete_links)
    _set_resolver_arguments(delete_links, [StrawberryArgument("inputs", None, StrawberryAnnotation(list[link_input]))])
