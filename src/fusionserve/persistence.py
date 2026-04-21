import logging
import re
from typing import Any, Literal

import inflect as _inflect
import yaml
from pydantic import ConfigDict, Field, create_model
from pydantic.alias_generators import to_pascal
from sqlalchemy import Column, MetaData, Select, Table, create_engine, func
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.automap import automap_base
from sqlalchemy.orm import DeclarativeMeta, load_only

from .auth import User
from .config import settings
from .models import RegistryItem, SmartComment

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


inflect = _inflect.engine()
inflect.classical(names=0)


def pydantic_field_from_column(
    column: Column,
    model_type: Literal["model", "get_input", "create_input", "pk_input"],
) -> tuple[Any, Field]:
    try:
        python_type = column.type.python_type
    except NotImplementedError:
        python_type = str
    field_type = {
        "model": python_type | None if column.nullable else python_type,
        "get_input": python_type | None,
        "create_input": python_type | None if not column.primary_key else python_type,
        "pk_input": python_type if column.primary_key else None,
    }[model_type]
    return (field_type, Field(None, description=column.comment))


def introspect():
    # Introspection is only supported for sync engines
    _engine = create_engine(
        f"postgresql+psycopg://{settings.pg_user}:{settings.pg_password.get_secret_value()}@"
        f"{settings.pg_host}:"
        f"{settings.pg_port}/{settings.pg_database}",
        echo=settings.echo_sql,
        pool_pre_ping=True,
    )
    metadata = MetaData()
    metadata.reflect(bind=_engine, schema=settings.pg_app_schema)
    models_registry: dict[str, RegistryItem] = {}
    Base = automap_base(metadata=metadata)
    # calling prepare() just sets up mapped classes and relationships.
    Base.prepare()
    for table in metadata.sorted_tables:
        if not inflect.singular_noun(table.name):
            raise ValueError(f"Table name {table.name} is not plural")
        item = RegistryItem()
        for model_type in RegistryItem.model_fields:
            setattr(
                item,
                model_type,
                create_model(
                    to_pascal(f"{inflect.singular_noun(table.name)}_{model_type}"),
                    __config__=ConfigDict(from_attributes=True),
                    **{
                        k: pydantic_field_from_column(v, model_type)
                        for k, v in table.columns.items()
                        if pydantic_field_from_column(v, model_type)[0]
                    },
                ),
            )
        models_registry[table.name] = item
    return Base, models_registry


async def set_role(session: AsyncSession, user: User | None):
    if not user:
        role = settings.anonymous_role
        statement = Select(func.set_config("role", role, True))
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
    _logger.debug(f"Setting role to {role}")
    await session.execute(statement)
    # select set_config('role', 'app_user', true), set_config('user_id', '2', true), ...


# Compiled once at module level; reusing compiled objects avoids per-call overhead.
_FRONTMATTER_PATTERN = re.compile(r"^---\s*$.*^---\s*$.*", re.MULTILINE | re.DOTALL)
_FRONTMATTER_BOUNDARY = re.compile(r"^---\s*$", re.MULTILINE)


def parse_comments(table: Table) -> SmartComment:
    """Parse a table comment, extracting optional YAML frontmatter metadata.

    If the comment starts with a YAML frontmatter block delimited by ``---``
    markers, the metadata is parsed and returned alongside the plain-text
    content.  Any YAML parse error falls back to returning the whole comment as
    plain-text content — no exception is raised (per the parsing contract).

    Args:
        table: SQLAlchemy ``Table`` whose ``comment`` attribute is parsed.

    Returns:
        A :class:`~fusionserve.models.SmartComment` with optional ``metadata``
        and ``content`` fields populated.
    """
    if not table.comment:
        return SmartComment()

    if not _FRONTMATTER_PATTERN.fullmatch(table.comment):
        return SmartComment(content=table.comment)

    _, frontmatter, content = _FRONTMATTER_BOUNDARY.split(table.comment, 2)

    try:
        metadata = yaml.safe_load(frontmatter)
    except yaml.YAMLError:
        return SmartComment(content=table.comment)

    return SmartComment(metadata=metadata, content=content.lstrip("\n"))


def apply_load_only(statement: Select, table: DeclarativeMeta, selected_fields: list[str] | None):
    if selected_fields:
        columns = [getattr(table, column) for column in selected_fields]

    else:
        columns = [getattr(table, column.name) for column in table.__table__.primary_key.columns]
    return statement.options(load_only(*columns))
