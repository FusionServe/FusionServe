"""Unit tests for smart-comment ``exclude`` gating on the GraphQL mutation surface.

Only the pure, DB-free gating paths are exercised here (the full schema build
needs a live PostgreSQL introspection, covered by the integration suite).
"""

from __future__ import annotations

from sqlalchemy import Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from fusionserve.graphql import _attach_mutations


def _orm_class(comment: str | None = None, name: str = "widgets"):
    class Base(DeclarativeBase):
        pass

    class Widget(Base):
        __tablename__ = name
        __table_args__ = {"comment": comment}

        id: Mapped[int] = mapped_column(Integer, primary_key=True)
        label: Mapped[str] = mapped_column(String, nullable=False)

    return Widget


def test_attach_mutations_returns_false_when_all_crud_excluded():
    class Mutation:
        pass

    # ``exclude: true`` suppresses create/update/delete, so ``_attach_mutations``
    # early-returns after reading the table's smart comment -- before touching
    # ``orm``/``gql_type``, which can therefore stay ``None``.
    orm_class = _orm_class("---\nexclude: true\n---\n")
    result = _attach_mutations(Mutation, None, orm_class, None)

    assert result is False
    assert not [name for name in vars(Mutation) if not name.startswith("__")]
