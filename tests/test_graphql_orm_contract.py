"""Guard tests for strawberry-orm internals the dynamic GraphQL builder relies on.

These run without a database. They pin the private strawberry-orm backend API
(`_filter_registry` / `_order_registry`) and the context stash slots so that a
strawberry-orm upgrade which changes those internals fails loudly here instead
of silently dropping filter/order arguments or breaking relay connections.
"""

from __future__ import annotations

import datetime
import typing
import uuid

import pytest
from sqlalchemy import Date, DateTime, Time, Uuid
from sqlalchemy.dialects.postgresql.asyncpg import dialect as AsyncpgDialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from strawberry_orm import StrawberryORM
from strawberry_orm.backends import sqlalchemy as sa_backend
from strawberry_orm.filters import ReferenceLookup

from fusionserve import graphql
from fusionserve.models import (
    FILTER_OVERRIDES,
    DateComparisonLookup,
    DateTimeComparisonLookup,
    TimeComparisonLookup,
    UUIDComparisonLookup,
)


def _backend():
    orm = StrawberryORM.for_sqlalchemy(dialect="postgresql", session_getter=lambda info: None)
    return orm.backend


class _Base(DeclarativeBase):
    pass


class _Event(_Base):
    __tablename__ = "events"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    owner_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    starts_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))
    on_date: Mapped[datetime.date] = mapped_column(Date)
    at_time: Mapped[datetime.time] = mapped_column(Time)


def test_filter_and_order_registries_present():
    """The private per-model filter/order registries still exist on the backend."""
    backend = _backend()
    assert graphql._orm_registry(backend, graphql._FILTER_REGISTRY_ATTR) is not None
    assert graphql._orm_registry(backend, graphql._ORDER_REGISTRY_ATTR) is not None


def test_orm_registry_raises_on_missing_attribute():
    """The guard raises a clear error when a relied-upon attribute disappears."""

    class _Stub:
        pass

    with pytest.raises(RuntimeError, match="missing the private"):
        graphql._orm_registry(_Stub(), "_filter_registry")


def test_filter_overrides_map_date_time_uuid_columns():
    """date/time/uuid columns resolve to the concrete-typed lookup overrides.

    The default strawberry-orm lookups type these as ``str`` (and uuid maps to
    ``str``), which under asyncpg's bind casts renders ``col >= $1::VARCHAR`` and
    PostgreSQL rejects the operator. The overrides keep the column type.
    """
    orm = StrawberryORM.for_sqlalchemy(
        dialect="postgresql",
        session_getter=lambda info: None,
        filter_overrides=FILTER_OVERRIDES,
    )
    orm.filter(_Event)
    filter_type = orm.backend._filter_registry.get(_Event)
    hints = typing.get_type_hints(filter_type._field_type)

    assert hints["starts_at"] == (DateTimeComparisonLookup | None)
    assert hints["on_date"] == (DateComparisonLookup | None)
    assert hints["at_time"] == (TimeComparisonLookup | None)
    # Non-PK uuid column uses the uuid lookup (relies on the _SA_TYPE_MAP patch).
    assert hints["owner_id"] == (UUIDComparisonLookup | None)


def test_datetime_lookup_binds_as_timestamp_not_varchar():
    """A datetime lookup renders ``$1::TIMESTAMP …`` (not ``$1::VARCHAR``)."""
    lookup = DateTimeComparisonLookup(gte=datetime.datetime(2024, 1, 1))
    clauses = sa_backend._build_lookup_clauses(_Event.starts_at, lookup)
    clause = clauses[0]
    assert clause.right.type._type_affinity is DateTime
    sql = str(clause.compile(dialect=AsyncpgDialect()))
    assert "VARCHAR" not in sql
    assert "TIMESTAMP" in sql


def test_uuid_reference_lookup_coerces_and_binds_as_uuid():
    """uuid PK / FK filters route through ReferenceLookup and bind as UUID.

    Verifies the ``_coerce_reference_value`` patch: a uuid string becomes a real
    ``uuid.UUID`` so the clause renders ``$1::UUID`` instead of ``$1::VARCHAR``.
    """
    value = "123e4567-e89b-12d3-a456-426614174000"
    clauses = sa_backend._build_lookup_clauses(_Event.id, ReferenceLookup(exact=value))
    clause = clauses[0]
    assert clause.right.value == uuid.UUID(value)
    assert clause.right.type._type_affinity is Uuid
    sql = str(clause.compile(dialect=AsyncpgDialect()))
    assert "VARCHAR" not in sql
    assert "UUID" in sql


def test_json_columns_typed_as_json_scalar_in_inputs():
    """The graphql import maps json/jsonb columns to the JSON scalar in input types.

    Without the patch the backend types JSON/JSONB mutation-input fields as
    ``str`` (``"JSON"`` -> str, ``"JSONB"`` absent -> str default), mismatching
    the JSON-scalar output type produced by ``_column_annotation``.
    """
    from strawberry.scalars import JSON as StrawberryJSON

    assert sa_backend._SA_TYPE_MAP["JSON"] is StrawberryJSON
    assert sa_backend._SA_TYPE_MAP["JSONB"] is StrawberryJSON


def test_uuid_patch_applied_to_backend_internals():
    """The graphql import patches the backend's uuid type map + reference coercion."""
    assert sa_backend._SA_TYPE_MAP["UUID"] is uuid.UUID
    assert sa_backend._coerce_reference_value("42") == 42
    assert sa_backend._coerce_reference_value("123e4567-e89b-12d3-a456-426614174000") == uuid.UUID(
        "123e4567-e89b-12d3-a456-426614174000"
    )
    assert sa_backend._coerce_reference_value("not-a-uuid") == "not-a-uuid"


def test_uuid_patch_raises_on_missing_internal(monkeypatch):
    """The guard raises a clear error if a relied-upon backend symbol disappears."""
    monkeypatch.delattr(sa_backend, "_coerce_reference_value", raising=True)
    with pytest.raises(RuntimeError, match="_coerce_reference_value"):
        graphql._patch_strawberry_orm_uuid()


def test_nodes_added_to_optimizer_passthrough():
    """Importing connections extends the optimizer passthrough set with ``nodes``.

    This lets the flat ``nodes { … }`` connection shape eager-load nested to-one
    relations (the lib ships with only ``edges``/``node``). Pins the internal so
    a strawberry-orm change is caught.
    """
    from strawberry_orm.optimizer import selections

    import fusionserve.connections  # noqa: F401  (import side effect)

    assert "nodes" in selections._RELAY_PASSTHROUGH_FIELDS


def test_custom_context_exposes_orm_stash_slots():
    """CustomContext declares the slots the orm filter/order + relay machinery writes."""
    ctx = graphql.CustomContext(session=None)
    # All four must be assignable (BaseContext is a slotted msgspec Struct).
    ctx._orm_base_query = object()
    ctx._orm_backend = object()
    ctx._orm_group_by = None
    ctx._orm_order = None
