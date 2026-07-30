"""Unit tests for smart-comment ``deprecated`` (hybrid: option A).

DB-free: exercises the deprecation helpers and ``_apply_descriptions`` against a
small hand-built Strawberry type + SQLAlchemy table. Object types (tables) carry
the ``@deprecated`` schema directive (SDL-only, since introspection has no
``__Type.isDeprecated``); fields use the built-in ``deprecation_reason`` so they
surface via both SDL and introspection (``isDeprecated`` / ``deprecationReason``).
"""

import json

import strawberry
from sqlalchemy import Column, Integer, MetaData, String, Table
from strawberry.annotation import StrawberryAnnotation

from fusionserve.graphql import (
    Deprecated,
    _apply_descriptions,
    _mark_field_deprecated,
    _mark_type_deprecated,
)


def _widget_type():
    @strawberry.type
    class Widget:
        id: int
        legacy: str
        modern: str

    return Widget


def _widget_table() -> Table:
    metadata = MetaData()
    return Table(
        "widgets",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("legacy", String, comment="---\ndeprecated: use modern\n---\nOld column.\n"),
        Column("modern", String, comment="The new column."),
        comment="---\ndeprecated: use widgets_v2\n---\nWidgets.\n",
    )


def test_mark_field_deprecated_sets_deprecation_reason_only_when_reason():
    field = strawberry.field(description="x")
    _mark_field_deprecated(field, None)
    assert field.deprecation_reason is None
    _mark_field_deprecated(field, "gone")
    assert field.deprecation_reason == "gone"


def test_mark_type_deprecated_appends_directive_only_when_reason():
    Widget = _widget_type()
    _mark_type_deprecated(Widget, None)
    assert list(Widget.__strawberry_definition__.directives) == []
    _mark_type_deprecated(Widget, "use v2")
    assert any(isinstance(d, Deprecated) and d.reason == "use v2" for d in Widget.__strawberry_definition__.directives)


def test_apply_descriptions_sets_type_and_column_deprecation():
    Widget = _widget_type()
    _apply_descriptions(Widget, _widget_table())
    definition = Widget.__strawberry_definition__

    # Table-level deprecation -> schema directive on the object type.
    assert any(isinstance(d, Deprecated) and d.reason == "use widgets_v2" for d in definition.directives)
    # Column-level deprecation -> built-in deprecation_reason; content -> description.
    legacy = definition.get_field("legacy")
    assert legacy.deprecation_reason == "use modern"
    assert legacy.description == "Old column.\n"
    # Non-deprecated column carries no deprecation.
    assert definition.get_field("modern").deprecation_reason is None


def _schema_with_deprecations():
    Widget = _widget_type()
    _apply_descriptions(Widget, _widget_table())

    class Query:
        pass

    Query.__annotations__ = {}
    widget_field = strawberry.field(resolver=lambda: Widget(id=1, legacy="a", modern="b"))
    widget_field.type_annotation = StrawberryAnnotation(Widget)
    _mark_field_deprecated(widget_field, "use widgets2")
    Query.widget = widget_field
    Query.__annotations__["widget"] = Widget
    return strawberry.Schema(query=strawberry.type(Query))


def test_deprecation_rendered_in_sdl():
    sdl = str(_schema_with_deprecations())
    assert "directive @deprecated" in sdl
    # Object-type deprecation via the custom directive.
    assert '@deprecated(reason: "use widgets_v2")' in sdl
    # Field deprecation via the built-in mechanism still prints @deprecated in SDL.
    assert '@deprecated(reason: "use modern")' in sdl
    assert '@deprecated(reason: "use widgets2")' in sdl


def test_field_deprecation_visible_via_introspection():
    schema = _schema_with_deprecations()

    widget = schema.execute_sync(
        '{ __type(name: "Widget") { fields(includeDeprecated: true) { name isDeprecated deprecationReason } } }'
    )
    assert widget.errors is None
    fields = {f["name"]: f for f in widget.data["__type"]["fields"]}
    assert fields["legacy"]["isDeprecated"] is True
    assert fields["legacy"]["deprecationReason"] == "use modern"
    assert fields["modern"]["isDeprecated"] is False

    query = schema.execute_sync(
        '{ __type(name: "Query") { fields(includeDeprecated: true) { name isDeprecated deprecationReason } } }'
    )
    assert query.errors is None
    widget_root = next(f for f in query.data["__type"]["fields"] if f["name"] == "widget")
    assert widget_root["isDeprecated"] is True
    assert widget_root["deprecationReason"] == "use widgets2"
    # Sanity: the introspection payload is JSON-serializable (no surprises).
    json.dumps(query.data)
