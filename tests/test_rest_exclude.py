"""Unit tests for smart-comment ``exclude`` gating on the REST surface.

These build lightweight declarative ORM classes (no DB / no automap prepare)
and drive :func:`fusionserve.rest.create_controller` / :func:`fusionserve.rest.build`
directly, asserting which CRUD handlers survive.
"""

from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy import Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from fusionserve import rest

_ALL_HANDLERS = {"list_items", "get_item", "create_item", "update_item", "delete_item"}


def _orm_class(comment: str | None = None, name: str = "widgets"):
    class Base(DeclarativeBase):
        pass

    class Widget(Base):
        __tablename__ = name
        __table_args__ = {"comment": comment}

        id: Mapped[int] = mapped_column(Integer, primary_key=True)
        label: Mapped[str] = mapped_column(String, nullable=False)

    return Widget


def _handlers(controller: type) -> set[str]:
    return {name for name in _ALL_HANDLERS if hasattr(controller, name)}


def test_no_exclude_keeps_all_handlers():
    controller = rest.create_controller(_orm_class(), is_view=False)
    assert _handlers(controller) == _ALL_HANDLERS


def test_exclude_read_drops_list_and_get():
    controller = rest.create_controller(_orm_class("---\nexclude: read\n---\n"), is_view=False)
    assert _handlers(controller) == {"create_item", "update_item", "delete_item"}


def test_exclude_create_drops_post():
    controller = rest.create_controller(_orm_class("---\nexclude: create\n---\n"), is_view=False)
    assert "create_item" not in _handlers(controller)
    assert {"list_items", "get_item", "update_item", "delete_item"} <= _handlers(controller)


def test_exclude_list_of_actions():
    controller = rest.create_controller(_orm_class("---\nexclude: [update, delete]\n---\n"), is_view=False)
    assert _handlers(controller) == {"list_items", "get_item", "create_item"}


def test_exclude_composes_with_view_strip():
    # Views drop writes anyway; excluding read on a view leaves nothing.
    controller = rest.create_controller(_orm_class("---\nexclude: read\n---\n"), is_view=True)
    assert _handlers(controller) == set()


def _introspection(*orm_classes, views: set[str] | None = None):
    return SimpleNamespace(base=SimpleNamespace(classes=list(orm_classes)), views=views or set())


def test_build_skips_fully_excluded_table():
    kept = _orm_class(name="kept")
    dropped = _orm_class("---\nexclude: true\n---\n", name="widgets")
    controllers = rest.build(_introspection(kept, dropped))
    assert len(controllers) == 1
    assert controllers[0].path.endswith("/kept")


def test_build_skips_view_with_read_excluded():
    view = _orm_class("---\nexclude: read\n---\n", name="active_widgets")
    controllers = rest.build(_introspection(view, views={"active_widgets"}))
    assert controllers == []


def test_build_keeps_partially_excluded_table():
    partial = _orm_class("---\nexclude: [create, delete]\n---\n", name="widgets")
    controllers = rest.build(_introspection(partial))
    assert len(controllers) == 1
    assert _handlers(controllers[0]) == {"list_items", "get_item", "update_item"}
