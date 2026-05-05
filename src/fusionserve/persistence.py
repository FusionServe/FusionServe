import datetime
import logging
import uuid
from collections import Counter
from decimal import Decimal
from typing import Any, Literal

import inflect as _inflect
from pydantic import Field, TypeAdapter
from sqlalchemy import DDL, Column, MetaData, Select, create_engine, func, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.automap import automap_base
from sqlalchemy.orm import DeclarativeMeta, load_only
from strawberry.scalars import JSON as StrawberryJSON

from .auth import User
from .config import settings
from .models import (
    FunctionInfo,
    FunctionSkip,
    Introspection,
    PgFunctionInfo,
)

_logger = logging.getLogger(settings.app_name)

engine = create_async_engine(
    f"postgresql+asyncpg://{settings.pg_user}:{settings.pg_password.get_secret_value()}@"
    f"{settings.pg_host}:"
    f"{settings.pg_port}/{settings.pg_database}",
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


def introspect() -> Introspection:
    """Reflect the configured PostgreSQL schema and return its full description.

    Uses a synchronous psycopg engine because SQLAlchemy reflection requires
    a sync dialect. Performs three things in order on the same engine:

    1. Installs the ``current_user_id()`` SQL function in
       ``settings.pg_app_schema`` (idempotently, on every startup).
    2. Reflects every table in that schema into a SQLAlchemy automap ``Base``.
    3. Discovers ``STABLE`` / ``IMMUTABLE`` functions in that schema via
       :func:`_introspect_functions` for the custom-query feature.

    Returns:
        An :class:`~fusionserve.models.Introspection` bundling the automap
        ``base`` with the list of supported ``functions``.

    Raises:
        ValueError: If any reflected table has a non-plural name.
    """
    # Introspection is only supported for sync engines.
    _engine = create_engine(
        f"postgresql+psycopg://{settings.pg_user}:{settings.pg_password.get_secret_value()}@"
        f"{settings.pg_host}:"
        f"{settings.pg_port}/{settings.pg_database}",
        echo=settings.echo_sql,
        pool_pre_ping=True,
    )
    schema = settings.pg_app_schema
    with _engine.begin() as connection:
        _logger.debug("Running DDL to create %s.current_user_id() function", schema)
        connection.execute(_current_user_id_ddl(schema))
    metadata = MetaData()
    metadata.reflect(bind=_engine, schema=schema)

    Base = automap_base(metadata=metadata)
    # calling prepare() just sets up mapped classes and relationships.
    Base.prepare()
    for table in metadata.sorted_tables:
        if not inflect.singular_noun(table.name):
            raise ValueError(f"Table name {table.name} is not plural")

    known_tables = {t.name for t in metadata.sorted_tables}
    with _engine.connect() as connection:
        functions = _introspect_functions(connection, schema, known_tables)
    return Introspection(base=Base, functions=functions)


async def set_role(session: AsyncSession, user: User | None):
    if not user:
        role = settings.anonymous_role
        statement = Select(func.set_config("role", role, True))
    else:
        role = user.role
        statement = Select(
            func.set_config("role", role, True),
            func.set_config("user.id", str(user.id), True),
            func.set_config("user.username", user.username, True),
            func.set_config("user.email", user.email or "", True),
            func.set_config("user.display_name", user.display_name or user.username, True),
            func.set_config("user.first_name", user.first_name or "", True),
            func.set_config("user.surname", user.surname or "", True),
        )
    _logger.debug("Setting role to %s", role)
    await session.execute(statement)
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
