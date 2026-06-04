import datetime
import logging
import uuid
from collections import Counter
from decimal import Decimal
from typing import Any, Literal

import inflect as _inflect
from pydantic import Field, TypeAdapter
from sqlalchemy import DDL, Column, MetaData, PrimaryKeyConstraint, Select, create_engine, func, inspect, text
from sqlalchemy.engine import URL, Connection
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.automap import (
    automap_base,
)
from sqlalchemy.ext.automap import (
    name_for_collection_relationship as _default_collection_name,
)
from sqlalchemy.ext.automap import (
    name_for_scalar_relationship as _default_scalar_name,
)
from sqlalchemy.orm import DeclarativeMeta, load_only
from sqlalchemy.sql.schema import ForeignKeyConstraint
from strawberry.scalars import JSON as StrawberryJSON

from .auth import User
from .config import settings
from .models import (
    FunctionInfo,
    FunctionSkip,
    Introspection,
    PgFunctionInfo,
    SmartComment,
)

_logger = logging.getLogger(settings.app_name)

db_url = URL.create(
    drivername="postgresql+asyncpg",
    username=settings.pg_user,
    password=settings.pg_password.get_secret_value(),
    host=settings.pg_host,
    port=settings.pg_port,
    database=settings.pg_database,
)

engine = create_async_engine(
    db_url,
    echo=settings.echo_sql,
    pool_pre_ping=True,
)

async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_async_session():
    async with async_session() as session:
        yield session


def _current_user_id_ddl(schema: str) -> DDL:
    """Build the ``CREATE OR REPLACE FUNCTION current_user_id()`` DDL.

    The function is materialised lazily (instead of at module import time)
    so that ``settings.pg_app_schema`` is read at the moment :func:`introspect`
    runs. This matters for tests that monkeypatch the schema after the
    ``persistence`` module has already been imported.
    """
    return DDL(
        f"""
        CREATE OR REPLACE FUNCTION {schema}.current_user_id()
        RETURNS uuid
        LANGUAGE sql
        STABLE
        AS $function$
          SELECT current_setting('user.id', true)::uuid;
        $function$;
        """
    )


#: Shared :mod:`inflect` engine used across the codebase. Constructed once
#: and configured with ``classical(names=0)`` so plurals follow modern
#: usage (e.g. ``persons`` rather than ``people``).
inflect = _inflect.engine()
inflect.classical(names=0)


def pydantic_field_from_column(
    column: Column,
    model_type: Literal["model", "get_input", "create_input"],
) -> tuple[Any, Field]:
    """Build a ``(type, Field)`` tuple for a Pydantic ``create_model`` call.

    The mapping from SQLAlchemy column to Pydantic field type depends on the
    role the generated model will play:

    * ``"model"`` — response payload: nullability mirrors the column's
      ``nullable`` flag.
    * ``"get_input"`` — query-string filter input: every field is optional.
    * ``"create_input"`` — POST request body for record creation: required
      and non-nullable when the column has neither a server default nor a
      Python-side default and is not nullable; optional otherwise.

    Args:
        column: The SQLAlchemy column to translate.
        model_type: Which Pydantic model variant the field will live in.

    Returns:
        A ``(field_type, Field)`` tuple suitable for splatting into
        :func:`pydantic.create_model`.
    """
    try:
        python_type = column.type.python_type
    except NotImplementedError:
        python_type = str
    has_default = column.server_default is not None or column.default is not None
    if model_type == "model":
        field_type = python_type | None if column.nullable else python_type
        default = None
    elif model_type == "get_input":
        field_type = python_type | None
        default = None
    elif model_type == "create_input":
        if column.nullable or has_default:
            field_type = python_type | None
            default = None
        else:
            field_type = python_type
            default = ...  # required
    else:
        raise ValueError(f"Unknown model_type {model_type!r}")
    return (field_type, Field(default, description=column.comment))


def _fk_constraints_to(local_table, referred_table) -> list[ForeignKeyConstraint]:
    """Return the FK constraints on ``local_table`` that reference ``referred_table``.

    Iterates ``local_table.foreign_key_constraints`` and keeps only those whose
    ``referred_table`` is the requested target. Used by the relationship
    naming callbacks to detect "multiple FKs to same target" situations.
    """
    return [fk for fk in local_table.foreign_key_constraints if fk.referred_table is referred_table]


def _scalar_name_from_constraint(constraint: ForeignKeyConstraint, local_table_name: str) -> str | None:
    """Derive a scalar relationship name from a FK constraint's own name.

    Applies the FK naming convention used in the schema:

    1. Strip ``<local_table_name>_`` prefix.
    2. Strip ``_fk`` or ``_fkey`` suffix (handles user conventions and
       PostgreSQL's default ``<table>_<column>_fkey``).
    3. Strip a trailing ``_id`` (handles the PostgreSQL default form).

    Worked examples:

    * ``posts_authors_fk`` -> ``authors``
    * ``messages_sender_id_fkey`` -> ``sender``
    * ``posts_author_fk`` -> ``author``

    Args:
        constraint: The FK constraint to inspect. Its ``name`` may be ``None``
            if the constraint was unnamed in the source schema.
        local_table_name: The name of the table that owns the constraint;
            used as the prefix to strip.

    Returns:
        The derived relationship name, or ``None`` if the constraint has no
        name or the derivation collapses to an empty string.
    """
    if not constraint.name:
        return None
    name = constraint.name.removeprefix(f"{local_table_name}_")
    for suffix in ("_fkey", "_fk", "_FKEY", "_FK"):
        stripped = name.removesuffix(suffix)
        if stripped != name:
            name = stripped
            break
    name = name.removesuffix("_id")
    return name or None


def _scalar_name_from_columns(constraint: ForeignKeyConstraint) -> str | None:
    """Derive a scalar relationship name from the FK's local columns.

    Fallback used when :func:`_scalar_name_from_constraint` returns ``None``.
    Joins the local column names with ``_`` and strips a trailing ``_id`` from
    the single-column case (the only one where stripping is unambiguous).
    """
    column_names = [c.name for c in constraint.columns]
    if not column_names:
        return None
    if len(column_names) == 1:
        name = column_names[0]
        if name.endswith("_id"):
            name = name[: -len("_id")]
        return name or None
    return "_".join(column_names) or None


def _disambiguate(name: str, taken: set[str]) -> str:
    """Return ``name`` (or ``name_2``, ``name_3``, ...) so it is not in ``taken``."""
    if name not in taken:
        return name
    counter = 2
    while f"{name}_{counter}" in taken:
        counter += 1
    return f"{name}_{counter}"


def _name_for_scalar_relationship(base, local_cls, referred_cls, constraint: ForeignKeyConstraint) -> str:
    """Automap callback: produce a scalar relationship name on ``local_cls``.

    For tables with a single FK to ``referred_cls`` this delegates to
    SQLAlchemy's :func:`name_for_scalar_relationship` so existing API
    contracts (e.g. ``Order.user``) are unchanged. For multi-FK cases the
    name is derived from the FK constraint name (see
    :func:`_scalar_name_from_constraint`) with a column-based fallback and a
    positional suffix as last resort.
    """
    local_table = local_cls.__table__
    referred_table = referred_cls.__table__
    siblings = _fk_constraints_to(local_table, referred_table)
    if len(siblings) <= 1:
        return _default_scalar_name(base, local_cls, referred_cls, constraint)
    derived = _scalar_name_from_constraint(constraint, local_table.name) or _scalar_name_from_columns(constraint)
    if not derived:
        derived = _default_scalar_name(base, local_cls, referred_cls, constraint)
    # Avoid collisions with already-attached relationships on ``local_cls``.
    taken: set[str] = set(local_cls.__mapper__.relationships.keys()) if hasattr(local_cls, "__mapper__") else set()
    return _disambiguate(derived, taken)


def _name_for_collection_relationship(base, local_cls, referred_cls, constraint: ForeignKeyConstraint) -> str:
    """Automap callback: produce a collection relationship name on ``local_cls``.

    Here ``local_cls`` is the *referred* side (the "one") of the FK and
    ``referred_cls`` is the source (the "many"). Single-FK cases delegate to
    SQLAlchemy's :func:`name_for_collection_relationship`. Multi-FK cases use
    ``<source_plural>_as_<scalar_name>`` so the two sides remain symmetric
    (e.g. ``User.messages_as_sender`` mirrors ``Message.sender``).
    """
    # In the collection callback, ``constraint`` lives on ``referred_cls``
    # (the "many" side) and points back to ``local_cls`` (the "one" side).
    source_table = referred_cls.__table__
    target_table = local_cls.__table__
    siblings = _fk_constraints_to(source_table, target_table)
    if len(siblings) <= 1:
        return _default_collection_name(base, local_cls, referred_cls, constraint)
    scalar = (
        _scalar_name_from_constraint(constraint, source_table.name)
        or _scalar_name_from_columns(constraint)
        or _default_scalar_name(base, referred_cls, local_cls, constraint)
    )
    # ``introspect()`` validates that every table name is plural, so using
    # ``source_table.name`` directly yields the desired ``<plural>_as_<role>``
    # shape (e.g. ``messages_as_sender``) without automap's ``_collection``
    # suffix that the default callback prepends.
    derived = f"{source_table.name}_as_{scalar}"
    taken: set[str] = set(local_cls.__mapper__.relationships.keys()) if hasattr(local_cls, "__mapper__") else set()
    return _disambiguate(derived, taken)


def _assign_view_primary_keys(metadata: MetaData, view_names: set[str]) -> set[str]:
    """Inject smart-comment-declared primary keys onto reflected view tables.

    SQLAlchemy's automap only maps a :class:`~sqlalchemy.schema.Table` that has
    a primary key, but (materialized or plain) views carry none in the
    PostgreSQL catalogue. This helper reads each view's smart comment and, when
    it declares a ``primary_key``, appends a matching
    :class:`~sqlalchemy.schema.PrimaryKeyConstraint` so the subsequent
    ``automap`` ``prepare()`` pass maps the view to an ORM class.

    Must be called **before** :func:`sqlalchemy.ext.automap.automap_base` is
    prepared. Views that declare no usable primary key are left untouched (and
    therefore stay unmapped) with a logged warning.

    Args:
        metadata: The reflected :class:`~sqlalchemy.schema.MetaData`.
        view_names: Names of reflected relations that are views (plain or
            materialized).

    Returns:
        The subset of ``view_names`` that received a primary key and will be
        mapped by automap.
    """
    mapped: set[str] = set()
    for table in metadata.sorted_tables:
        if table.name not in view_names:
            continue
        comment_metadata = SmartComment.from_object(table).metadata
        primary_key = comment_metadata.primary_key if comment_metadata else None
        if not primary_key:
            _logger.warning(
                "Skipping view %s: no primary_key declared in its smart comment; "
                "add a 'primary_key' frontmatter entry to expose it.",
                table.name,
            )
            continue
        missing = [name for name in primary_key if name not in table.columns]
        if missing:
            _logger.warning(
                "Skipping view %s: declared primary_key column(s) %s not found on the view.",
                table.name,
                missing,
            )
            continue
        table.append_constraint(PrimaryKeyConstraint(*primary_key))
        _logger.debug("Assigned primary key %s to view %s", primary_key, table.name)
        mapped.add(table.name)
    return mapped


def introspect() -> Introspection:
    """Reflect the configured PostgreSQL schema and return its full description.

    Uses a synchronous psycopg engine because SQLAlchemy reflection requires
    a sync dialect. Performs four things in order on the same engine:

    1. Installs the ``current_user_id()`` SQL function in
       ``settings.pg_app_schema`` (idempotently, on every startup).
    2. Reflects every table and view in that schema into a SQLAlchemy automap
       ``Base``. Views carry no primary key in the catalogue, so
       :func:`_assign_view_primary_keys` injects the one declared in each
       view's smart comment before ``automap`` is prepared; undeclared views
       stay unmapped.
    3. Discovers ``STABLE`` / ``IMMUTABLE`` functions in that schema via
       :func:`_introspect_functions` for the custom-query feature.

    Returns:
        An :class:`~fusionserve.models.Introspection` bundling the automap
        ``base`` with the list of supported ``functions`` and the set of
        mapped read-only ``views``.

    Raises:
        ValueError: If any reflected table has a non-plural name.
    """
    # Introspection is only supported for sync engines.
    _engine = create_engine(
        db_url.set(drivername="postgresql+psycopg"),
        echo=settings.echo_sql,
        pool_pre_ping=True,
    )
    schema = settings.pg_app_schema
    with _engine.begin() as connection:
        _logger.debug("Running DDL to create %s.current_user_id() function", schema)
        connection.execute(_current_user_id_ddl(schema))
        metadata = MetaData()
        metadata.reflect(bind=_engine, schema=schema, views=True)
        for table in metadata.sorted_tables:
            if not inflect.singular_noun(table.name):
                raise ValueError(f"Table name {table.name} is not plural")
        inspector = inspect(_engine)
        view_names = set(inspector.get_view_names(schema=schema)) | set(
            inspector.get_materialized_view_names(schema=schema)
        )
        views = _assign_view_primary_keys(metadata, view_names)
        base = automap_base(metadata=metadata)
        # Pass custom naming callbacks so tables with multiple FKs to the same
        # target get unique, semantically meaningful relationship names instead
        # of automap's positional ``user`` / ``user1`` suffixes (which collide
        # in the GraphQL schema and cause one relationship to be dropped).
        base.prepare(
            name_for_scalar_relationship=_name_for_scalar_relationship,
            name_for_collection_relationship=_name_for_collection_relationship,
        )
        known_tables = {t.name for t in metadata.sorted_tables}
        functions = _introspect_functions(connection, schema, known_tables)
    return Introspection(base=base, functions=functions, views=views)


def role_config_statement(user: User | None) -> Select:
    """Build the ``SELECT set_config(...)`` statement that applies a user's role.

    The configuration is **transaction-local** (``is_local=True``), so it is
    automatically discarded when the transaction ends — keeping pooled
    connections from carrying a role across requests. Because it is
    transaction-scoped, it must be re-applied on every new transaction within a
    request (see the GraphQL ``after_begin`` hook in
    :mod:`fusionserve.graphql`); a single ``await session.execute`` covers the
    common single-transaction case (see :func:`set_role`).

    Args:
        user: The authenticated user, or ``None`` for the anonymous role.

    Returns:
        A SQLAlchemy ``Select`` wrapping the ``set_config`` calls.
    """
    if not user:
        return Select(func.set_config("role", settings.anonymous_role, True))
    return Select(
        func.set_config("role", user.role, True),
        func.set_config("user.id", str(user.id), True),
        func.set_config("user.username", user.username, True),
        func.set_config("user.email", user.email or "", True),
        func.set_config("user.display_name", user.display_name or user.username, True),
        func.set_config("user.first_name", user.first_name or "", True),
        func.set_config("user.surname", user.surname or "", True),
    )


async def set_role(session: AsyncSession, user: User | None):
    """Apply the user's PostgreSQL role to ``session``'s current transaction."""
    role = settings.anonymous_role if not user else user.role
    _logger.debug("Setting role to %s", role)
    await session.execute(role_config_statement(user))
    # select set_config('role', 'app_user', true), set_config('user_id', '2', true), ...


#: Map of PostgreSQL ``pg_type.typname`` values to the Python / Strawberry
#: types we expose them as in the GraphQL and REST surfaces. Functions whose
#: arguments or return type are not in this map are skipped at introspection
#: time with a logged warning. ``json`` / ``jsonb`` use Strawberry's built-in
#: ``JSON`` scalar (lossless, arbitrary JSON) — only for function param /
#: return types; column-level json/jsonb mapping stays with the upstream
#: ``strawberry-sqlalchemy-mapper``.
_PG_TO_PY: dict[str, type] = {
    "int2": int,
    "int4": int,
    "int8": int,
    "float4": float,
    "float8": float,
    "numeric": Decimal,
    "text": str,
    "varchar": str,
    "bpchar": str,
    "name": str,
    "citext": str,
    "bool": bool,
    "uuid": uuid.UUID,
    "date": datetime.date,
    "time": datetime.time,
    "timetz": datetime.time,
    "timestamp": datetime.datetime,
    "timestamptz": datetime.datetime,
    "json": StrawberryJSON,
    "jsonb": StrawberryJSON,
}


#: SQL projection driving custom-query introspection. Selects the columns
#: required to populate :class:`~fusionserve.models.PgFunctionInfo` for every
#: ordinary STABLE/IMMUTABLE function in the requested schema.
_FUNCTIONS_SQL = text(
    """
    SELECT
        p.oid,
        p.proname,
        p.provolatile,
        p.proretset,
        p.prokind,
        p.proargnames,
        p.proargmodes,
        p.pronargs,
        p.pronargdefaults,
        COALESCE(
            array(
                SELECT t.typname
                FROM unnest(p.proargtypes) WITH ORDINALITY AS u(oid, ord)
                JOIN pg_type t ON t.oid = u.oid
                ORDER BY u.ord
            ),
            ARRAY[]::text[]
        ) AS arg_typnames,
        rt.typname AS return_typname,
        rt.typtype AS return_typtype,
        rt.typrelid AS return_typrelid,
        cl.relname AS return_relname,
        d.description AS comment
    FROM pg_proc p
    JOIN pg_namespace n ON n.oid = p.pronamespace
    JOIN pg_type rt ON rt.oid = p.prorettype
    LEFT JOIN pg_class cl ON cl.oid = rt.typrelid
    LEFT JOIN pg_description d ON d.objoid = p.oid AND d.objsubid = 0
    WHERE n.nspname = :schema
      AND p.provolatile IN ('s', 'i')
      AND p.prokind = 'f'
    ORDER BY p.proname, p.oid
    """
)


def _introspect_functions(
    connection: Connection,
    schema: str,
    known_tables: set[str],
) -> list[FunctionInfo]:
    """Discover STABLE / IMMUTABLE functions in ``schema`` and return their metadata.

    Pure orchestration: load raw rows via :class:`PgFunctionInfo`, detect
    overloads (skipped wholesale with a warning), and ask each row to project
    itself into a :class:`FunctionInfo` via
    :meth:`PgFunctionInfo.to_function_info`. Rows that cannot be projected
    return a :class:`FunctionSkip` instead — the skip is logged here, not in
    the model.

    Args:
        connection: A live synchronous SQLAlchemy connection bound to the
            target database.
        schema: The PostgreSQL schema to look in (typically
            ``settings.pg_app_schema``).
        known_tables: Set of table names already reflected by automap; used
            to validate row / set return types.

    Returns:
        A list of :class:`~fusionserve.models.FunctionInfo` — one per
        successfully introspected function.
    """
    pg_funcs = TypeAdapter(list[PgFunctionInfo]).validate_python(
        connection.execute(_FUNCTIONS_SQL, {"schema": schema}).mappings().all()
    )

    counts = Counter(p.proname for p in pg_funcs)
    overloaded = {name for name, n in counts.items() if n > 1}
    for name in sorted(overloaded):
        _logger.warning(
            "Skipping overloaded function %s.%s (multiple signatures defined; "
            "expose a single canonical version to enable custom-query support).",
            schema,
            name,
        )

    functions: list[FunctionInfo] = []
    for pg in pg_funcs:
        if pg.proname in overloaded:
            continue
        outcome = pg.to_function_info(schema, known_tables, _PG_TO_PY)
        if isinstance(outcome, FunctionSkip):
            _logger.warning("Skipping function %s.%s: %s", schema, pg.proname, outcome.message)
            continue
        functions.append(outcome)
    return functions


def apply_load_only(statement: Select, table: DeclarativeMeta, selected_fields: list[str] | None):
    if selected_fields:
        columns = [getattr(table, column) for column in selected_fields]
    else:
        columns = [getattr(table, column.name) for column in table.__table__.primary_key.columns]
    return statement.options(load_only(*columns))
