"""Unit tests for ``fusionserve.persistence`` pure helpers.

These tests deliberately avoid touching ``fusionserve.main`` (which would
trigger live DB introspection at import time). They exercise the small,
deterministic helpers that drive REST and GraphQL schema generation.
"""

from __future__ import annotations

from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    func,
    text,
)

from fusionserve.models import SmartComment
from fusionserve.persistence import parse_comments, pydantic_field_from_column


def _make_table(comment: str | None = None) -> Table:
    metadata = MetaData()
    return Table(
        "users",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("email", String, nullable=False),
        Column("name", String, nullable=True),
        Column("created_at", DateTime, server_default=func.now()),
        Column("counter", Integer, default=0, nullable=False),
        comment=comment,
    )


# --- parse_comments ----------------------------------------------------------


def test_parse_comments_no_comment_returns_empty_smartcomment():
    table = _make_table(comment=None)
    result = parse_comments(table)
    assert isinstance(result, SmartComment)
    assert result.metadata is None
    assert result.content is None


def test_parse_comments_plain_text_only_populates_content():
    table = _make_table(comment="Just some prose, no frontmatter.")
    result = parse_comments(table)
    assert result.metadata is None
    assert result.content == "Just some prose, no frontmatter."


def test_parse_comments_with_frontmatter_splits_metadata_and_content():
    comment = "---\nrole: admin\nlabel: Users\n---\nThe users table.\n"
    table = _make_table(comment=comment)
    result = parse_comments(table)
    assert result.metadata == {"role": "admin", "label": "Users"}
    assert result.content == "The users table.\n"


def test_parse_comments_with_invalid_yaml_falls_back_to_plain_content():
    # Unbalanced quoting yields a YAMLError; the contract says fall back to
    # treating the whole comment as plain content.
    comment = '---\nrole: "admin\n---\nrest of body\n'
    table = _make_table(comment=comment)
    result = parse_comments(table)
    assert result.metadata is None
    assert result.content == comment


# --- pydantic_field_from_column ---------------------------------------------


def test_pydantic_field_model_mode_respects_nullability():
    table = _make_table()
    nullable_type, _ = pydantic_field_from_column(table.c.name, "model")
    non_nullable_type, _ = pydantic_field_from_column(table.c.email, "model")
    assert nullable_type == str | None
    assert non_nullable_type is str


def test_pydantic_field_get_input_mode_makes_everything_optional():
    table = _make_table()
    for col in table.columns:
        field_type, field = pydantic_field_from_column(col, "get_input")
        assert type(None) in field_type.__args__, f"{col.name} must be optional"
        assert field.default is None


def test_pydantic_field_create_input_required_when_no_default_and_not_nullable():
    table = _make_table()
    field_type, field = pydantic_field_from_column(table.c.email, "create_input")
    # email is non-nullable, no default → required
    assert field_type is str
    assert field.is_required()


def test_pydantic_field_create_input_optional_when_nullable():
    table = _make_table()
    field_type, field = pydantic_field_from_column(table.c.name, "create_input")
    assert field_type == str | None
    assert field.default is None


def test_pydantic_field_create_input_optional_when_server_default():
    table = _make_table()
    field_type, field = pydantic_field_from_column(table.c.created_at, "create_input")
    # column is non-nullable but has server_default — should still be optional
    assert type(None) in field_type.__args__
    assert field.default is None


def test_pydantic_field_create_input_optional_when_python_default():
    table = _make_table()
    field_type, field = pydantic_field_from_column(table.c.counter, "create_input")
    assert type(None) in field_type.__args__
    assert field.default is None


def test_pydantic_field_unknown_python_type_falls_back_to_str():
    metadata = MetaData()
    # CITEXT-style: a custom user-defined type whose python_type is not impl.
    from sqlalchemy.types import UserDefinedType

    class _NoPython(UserDefinedType):
        cache_ok = True

        def get_col_spec(self, **_):
            return "MYTYPE"

    table = Table("t", metadata, Column("x", _NoPython(), nullable=True))
    field_type, _ = pydantic_field_from_column(table.c.x, "model")
    assert field_type == str | None


def test_pydantic_field_server_default_text_works_too():
    metadata = MetaData()
    table = Table(
        "t",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("flag", Integer, server_default=text("0"), nullable=False),
    )
    field_type, field = pydantic_field_from_column(table.c.flag, "create_input")
    assert type(None) in field_type.__args__
    assert field.default is None
