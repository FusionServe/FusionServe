import logging
from typing import Any, Literal

import inflect as _inflect
from pydantic import ConfigDict, Field, create_model
from pydantic.alias_generators import to_pascal
from sqlalchemy import Column, MetaData, create_engine, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.automap import automap_base

from .config import settings
from .models import RegistryItem

_logger = logging.getLogger(settings.app_name)

engine = create_async_engine(
    f"postgresql+asyncpg://{settings.pg_user}:{settings.pg_password}@"
    f"{settings.pg_host}:"
    f"{'5432'}/{settings.pg_database}",
    echo=settings.echo_sql,
    pool_pre_ping=True,
)


async def get_async_session():
    async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
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
        f"postgresql+psycopg://{settings.pg_user}:{settings.pg_password}@"
        f"{settings.pg_host}:"
        f"{'5432'}/{settings.pg_database}",
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


async def set_role(session: AsyncSession):
    # TODO: role from jwt or anonymous
    role = settings.anonymous_role
    _logger.debug(f"Setting role to {role}")
    await session.execute(text(f"SET ROLE '{role}'"))
