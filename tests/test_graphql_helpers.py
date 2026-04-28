"""Unit tests for the pure helpers in ``fusionserve.graphql``.

The dynamic-schema ``build()`` function requires a live PostgreSQL
introspection result, so it is exercised in the integration tests. The
helpers below are synchronous, deterministic, and ORM-only — perfect for
fast unit coverage.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
import strawberry
from sqlalchemy import Column, ForeignKey, Integer, String
from sqlalchemy.orm import declarative_base

from fusionserve.graphql import (
    _MAX_WHERE_DEPTH,
    apply_order_by,
    apply_where,
    columns_from_selections,
    create_order_by_input,
    create_where_input,
)
from fusionserve.models import SortDirection

Base = declarative_base()


class Author(Base):
    __tablename__ = "authors"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)


class Book(Base):
    __tablename__ = "books"
    id = Column(Integer, primary_key=True)
    title = Column(String, nullable=False)
    author_id = Column(Integer, ForeignKey("authors.id"))


# --- columns_from_selections ------------------------------------------------


@dataclass
class _SelectedField:
    """Minimal stand-in for ``strawberry.types.nodes.SelectedField``.

    Only the attributes that ``columns_from_selections`` actually reads are
    populated; the production type is a dataclass with the same shape.
    """

    name: str
    selections: list[Any] = field(default_factory=list)


@dataclass
class _FragmentSpread:
    name: str
    selections: list[Any] = field(default_factory=list)


def test_columns_from_selections_picks_explicit_scalar_fields(monkeypatch):
    monkeypatch.setattr("strawberry.types.nodes.SelectedField", _SelectedField)
    monkeypatch.setattr("strawberry.types.nodes.FragmentSpread", _FragmentSpread)
    selections = [_SelectedField(name="title"), _SelectedField(name="id")]
    result = columns_from_selections(selections, Book)
    # FK column author_id is always appended.
    assert "title" in result
    assert "id" in result
    assert "author_id" in result


def test_columns_from_selections_ignores_unknown_field_names(monkeypatch):
    monkeypatch.setattr("strawberry.types.nodes.SelectedField", _SelectedField)
    monkeypatch.setattr("strawberry.types.nodes.FragmentSpread", _FragmentSpread)
    selections = [_SelectedField(name="not_a_column")]
    result = columns_from_selections(selections, Book)
    # Only FK auto-include remains.
    assert result == ["author_id"]


def test_columns_from_selections_recurses_into_nested(monkeypatch):
    monkeypatch.setattr("strawberry.types.nodes.SelectedField", _SelectedField)
    monkeypatch.setattr("strawberry.types.nodes.FragmentSpread", _FragmentSpread)
    selections = [
        _SelectedField(
            name="author",
            selections=[_SelectedField(name="name"), _SelectedField(name="id")],
        ),
    ]
    # `author` is not a column on Book, but its sub-selections are columns on
    # Author — those still get walked. The function tests against the
    # *current* table though, so "name" won't match Book columns. The FK
    # author_id is the only Book column added regardless.
    result = columns_from_selections(selections, Book)
    assert "author_id" in result


def test_columns_from_selections_handles_fragment_spread(monkeypatch):
    monkeypatch.setattr("strawberry.types.nodes.SelectedField", _SelectedField)
    monkeypatch.setattr("strawberry.types.nodes.FragmentSpread", _FragmentSpread)
    fragment = _FragmentSpread(
        name="BookFields",
        selections=[_SelectedField(name="title"), _SelectedField(name="id")],
    )
    result = columns_from_selections([fragment], Book)
    # FragmentSpread sub-fields are added without column-existence checks.
    assert "title" in result
    assert "id" in result


# --- apply_order_by ---------------------------------------------------------


def test_apply_order_by_appends_clauses_for_set_fields():
    OrderBy = create_order_by_input(Book.__table__)
    order_by = OrderBy(title=SortDirection.ASC, id=SortDirection.DESC_NULLS_LAST)
    from sqlalchemy import select

    statement = select(Book)
    statement = apply_order_by(statement, Book, order_by)
    compiled = statement.compile(compile_kwargs={"literal_binds": True})
    sql = str(compiled)
    assert "ORDER BY" in sql
    assert "title ASC" in sql
    assert "id DESC NULLS LAST" in sql


def test_apply_order_by_skips_unset_fields():
    OrderBy = create_order_by_input(Book.__table__)
    order_by = OrderBy()
    from sqlalchemy import select

    statement = select(Book)
    statement = apply_order_by(statement, Book, order_by)
    compiled = statement.compile(compile_kwargs={"literal_binds": True})
    assert "ORDER BY" not in str(compiled)


# --- apply_where ------------------------------------------------------------


def test_apply_where_returns_none_when_no_conditions():
    Where = create_where_input(Book.__table__)
    assert apply_where(Book, Where()) is None


def test_apply_where_builds_conjunction_for_multiple_columns():
    Where = create_where_input(Book.__table__)
    # Build comparison-input instances for the two columns.
    title_field = Where.__annotations__["title"].__args__[0]
    id_field = Where.__annotations__["id"].__args__[0]
    where = Where(title=title_field(eq="The Trial"), id=id_field(gt=1))
    condition = apply_where(Book, where)
    assert condition is not None
    sql = str(condition.compile(compile_kwargs={"literal_binds": True}))
    assert "books.title = 'The Trial'" in sql
    assert "books.id > 1" in sql
    assert " AND " in sql


def test_apply_where_supports_combinators():
    Where = create_where_input(Book.__table__)
    title_field = Where.__annotations__["title"].__args__[0]
    where = Where(
        _or=[
            Where(title=title_field(eq="A")),
            Where(title=title_field(eq="B")),
        ]
    )
    condition = apply_where(Book, where)
    assert condition is not None
    sql = str(condition.compile(compile_kwargs={"literal_binds": True}))
    assert "OR" in sql
    assert "'A'" in sql
    assert "'B'" in sql


def test_apply_where_max_depth_raises():
    Where = create_where_input(Book.__table__)
    title_field = Where.__annotations__["title"].__args__[0]
    # Wrap in nested _and beyond the depth limit.
    where = Where(title=title_field(eq="x"))
    for _ in range(_MAX_WHERE_DEPTH + 1):
        where = Where(_and=[where])
    with pytest.raises(ValueError, match="exceeds maximum depth"):
        apply_where(Book, where)


def test_apply_where_is_null_operator():
    Where = create_where_input(Book.__table__)
    author_id_field = Where.__annotations__["author_id"].__args__[0]
    where = Where(author_id=author_id_field(is_null=True))
    condition = apply_where(Book, where)
    assert condition is not None
    sql = str(condition.compile(compile_kwargs={"literal_binds": True}))
    assert "IS NULL" in sql.upper()


# silence unused-import warnings when strawberry decorators are imported but
# evaluated lazily by the helpers.
_ = strawberry
