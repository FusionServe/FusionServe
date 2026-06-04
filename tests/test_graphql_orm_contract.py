"""Guard tests for strawberry-orm internals the dynamic GraphQL builder relies on.

These run without a database. They pin the private strawberry-orm backend API
(`_filter_registry` / `_order_registry`) and the context stash slots so that a
strawberry-orm upgrade which changes those internals fails loudly here instead
of silently dropping filter/order arguments or breaking relay connections.
"""

from __future__ import annotations

from strawberry_orm import StrawberryORM

from fusionserve import graphql


def _backend():
    orm = StrawberryORM.for_sqlalchemy(dialect="postgresql", session_getter=lambda info: None)
    return orm.backend


def test_filter_and_order_registries_present():
    """The private per-model filter/order registries still exist on the backend."""
    backend = _backend()
    assert graphql._orm_registry(backend, graphql._FILTER_REGISTRY_ATTR) is not None
    assert graphql._orm_registry(backend, graphql._ORDER_REGISTRY_ATTR) is not None


def test_orm_registry_raises_on_missing_attribute():
    """The guard raises a clear error when a relied-upon attribute disappears."""
    import pytest

    class _Stub:
        pass

    with pytest.raises(RuntimeError, match="missing the private"):
        graphql._orm_registry(_Stub(), "_filter_registry")


def test_custom_context_exposes_orm_stash_slots():
    """CustomContext declares the slots the orm filter/order + relay machinery writes."""
    ctx = graphql.CustomContext(session=None)
    # All four must be assignable (BaseContext is a slotted msgspec Struct).
    ctx._orm_base_query = object()
    ctx._orm_backend = object()
    ctx._orm_group_by = None
    ctx._orm_order = None
