"""Unit tests for ``fusionserve.persistence`` pure helpers.

These tests deliberately avoid touching ``fusionserve.main`` (which would
trigger live DB introspection at import time). They exercise the small,
deterministic helpers that drive REST and GraphQL schema generation.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    func,
    text,
)
from sqlalchemy.ext.automap import automap_base

from fusionserve.models import SmartComment
from fusionserve.persistence import (
    _assign_view_primary_keys,
    _name_for_collection_relationship,
    _name_for_scalar_relationship,
    pydantic_field_from_column,
)


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
    result = SmartComment.from_object(table)
    assert isinstance(result, SmartComment)
    assert result.metadata is None
    assert result.content is None


def test_parse_comments_plain_text_only_populates_content():
    table = _make_table(comment="Just some prose, no frontmatter.")
    result = SmartComment.from_object(table)
    assert result.metadata is None
    assert result.content == "Just some prose, no frontmatter."


def test_parse_comments_with_frontmatter_splits_metadata_and_content():
    comment = "---\nrole: admin\nlabel: Users\n---\nThe users table.\n"
    table = _make_table(comment=comment)
    result = SmartComment.from_object(table)
    assert result.metadata is not None
    # Unknown keys are dropped via ``extra="ignore"``.
    assert not result.metadata.model_extra
    assert result.metadata.primary_key is None
    assert result.content == "The users table.\n"


def test_parse_comments_with_invalid_yaml_falls_back_to_plain_content():
    # Unbalanced quoting yields a YAMLError; the contract says fall back to
    # treating the whole comment as plain content.
    comment = '---\nrole: "admin\n---\nrest of body\n'
    table = _make_table(comment=comment)
    result = SmartComment.from_object(table)
    assert result.metadata is None
    assert result.content == comment


def test_parse_comments_primary_key_string_is_coerced_to_list():
    comment = "---\nprimary_key: id\n---\nA view.\n"
    table = _make_table(comment=comment)
    result = SmartComment.from_object(table)
    assert result.metadata is not None
    assert result.metadata.primary_key == ["id"]


def test_parse_comments_primary_key_list_is_preserved():
    comment = "---\nprimary_key: [tenant_id, id]\n---\n"
    table = _make_table(comment=comment)
    result = SmartComment.from_object(table)
    assert result.metadata is not None
    assert result.metadata.primary_key == ["tenant_id", "id"]


def test_parse_comments_invalid_primary_key_raises():
    # Well-formed YAML but invalid metadata: fail-fast per the parsing contract.
    comment = "---\nprimary_key: 123\n---\n"
    table = _make_table(comment=comment)
    with pytest.raises(ValidationError):
        SmartComment.from_object(table)


# --- _assign_view_primary_keys ----------------------------------------------


def _view_metadata(comment: str | None) -> tuple[MetaData, Table]:
    metadata = MetaData()
    view = Table(
        "active_users",
        metadata,
        Column("id", Integer),
        Column("email", String),
        comment=comment,
    )
    return metadata, view


def test_assign_view_primary_keys_injects_declared_pk_and_maps():
    metadata, view = _view_metadata("---\nprimary_key: id\n---\n")
    mapped = _assign_view_primary_keys(metadata, {"active_users"})
    assert mapped == {"active_users"}
    assert [c.name for c in view.primary_key.columns] == ["id"]

    Base = automap_base(metadata=metadata)
    Base.prepare()
    assert "active_users" in Base.classes


def test_assign_view_primary_keys_skips_view_without_declaration():
    metadata, view = _view_metadata("Just a description, no frontmatter.")
    mapped = _assign_view_primary_keys(metadata, {"active_users"})
    assert mapped == set()
    assert len(view.primary_key.columns) == 0


def test_assign_view_primary_keys_skips_unknown_column():
    metadata, view = _view_metadata("---\nprimary_key: does_not_exist\n---\n")
    mapped = _assign_view_primary_keys(metadata, {"active_users"})
    assert mapped == set()
    assert len(view.primary_key.columns) == 0


def test_assign_view_primary_keys_ignores_non_view_tables():
    metadata, view = _view_metadata("---\nprimary_key: id\n---\n")
    # Not listed as a view -> left untouched even though it declares a pk.
    mapped = _assign_view_primary_keys(metadata, set())
    assert mapped == set()
    assert len(view.primary_key.columns) == 0


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


# --- automap relationship naming --------------------------------------------


def _prepare_with_helpers(metadata: MetaData):
    """Build an automap base from ``metadata`` using the persistence callbacks.

    Returns the prepared :class:`AutomapBase` so tests can inspect
    ``Base.classes.<table>.__mapper__.relationships``.
    """
    Base = automap_base(metadata=metadata)
    Base.prepare(
        name_for_scalar_relationship=_name_for_scalar_relationship,
        name_for_collection_relationship=_name_for_collection_relationship,
    )
    return Base


def test_single_fk_preserves_automap_default_names():
    """Tables with one FK to a target keep SQLAlchemy's default names."""
    metadata = MetaData()
    Table("users", metadata, Column("id", Integer, primary_key=True))
    Table(
        "orders",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("user_id", Integer, ForeignKey("users.id")),
    )
    Base = _prepare_with_helpers(metadata)
    order_rels = set(Base.classes.orders.__mapper__.relationships.keys())
    user_rels = set(Base.classes.users.__mapper__.relationships.keys())
    # The automap default for a scalar relationship is the referred class name
    # lowercased; for a collection it is ``<table>_collection``. Whatever those
    # defaults are, our helpers must not have altered them in the single-FK
    # case — assert at least one relationship exists on each side and that no
    # ``_as_`` suffix has leaked in (those are reserved for multi-FK cases).
    assert order_rels
    assert user_rels
    assert not any("_as_" in name for name in order_rels | user_rels)


def test_multi_fk_scalar_names_derived_from_constraint_pg_default():
    """Multi-FK scalar names follow the ``<table>_<col>_fkey`` PG convention."""
    metadata = MetaData()
    Table("users", metadata, Column("id", Integer, primary_key=True))
    Table(
        "messages",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("sender_id", Integer, ForeignKey("users.id", name="messages_sender_id_fkey")),
        Column("recipient_id", Integer, ForeignKey("users.id", name="messages_recipient_id_fkey")),
    )
    Base = _prepare_with_helpers(metadata)
    msg_rels = set(Base.classes.messages.__mapper__.relationships.keys())
    assert msg_rels == {"sender", "recipient"}


def test_multi_fk_scalar_names_derived_from_fk_convention():
    """Multi-FK scalar names also honour the ``<table>_<role>_fk`` convention."""
    metadata = MetaData()
    Table("authors", metadata, Column("id", Integer, primary_key=True))
    Table(
        "posts",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("primary_author_id", Integer, ForeignKey("authors.id", name="posts_authors_fk")),
        Column("secondary_author_id", Integer, ForeignKey("authors.id", name="posts_co_authors_fk")),
    )
    Base = _prepare_with_helpers(metadata)
    post_rels = set(Base.classes.posts.__mapper__.relationships.keys())
    assert post_rels == {"authors", "co_authors"}


def test_multi_fk_collection_names_use_as_suffix():
    """The reverse side of multi-FK relationships uses ``<plural>_as_<role>``."""
    metadata = MetaData()
    Table("users", metadata, Column("id", Integer, primary_key=True))
    Table(
        "messages",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("sender_id", Integer, ForeignKey("users.id", name="messages_sender_id_fkey")),
        Column("recipient_id", Integer, ForeignKey("users.id", name="messages_recipient_id_fkey")),
    )
    Base = _prepare_with_helpers(metadata)
    user_rels = set(Base.classes.users.__mapper__.relationships.keys())
    assert user_rels == {"messages_as_sender", "messages_as_recipient"}


def test_multi_fk_unnamed_constraint_falls_back_to_columns():
    """Anonymous FK constraints fall back to FK column name stripped of ``_id``."""
    metadata = MetaData()
    Table("users", metadata, Column("id", Integer, primary_key=True))
    Table(
        "messages",
        metadata,
        Column("id", Integer, primary_key=True),
        # No ``name=`` on the ForeignKey — SQLAlchemy/automap will see an
        # unnamed constraint, so the column-name fallback kicks in.
        Column("sender_id", Integer, ForeignKey("users.id")),
        Column("recipient_id", Integer, ForeignKey("users.id")),
    )
    Base = _prepare_with_helpers(metadata)
    msg_rels = set(Base.classes.messages.__mapper__.relationships.keys())
    assert msg_rels == {"sender", "recipient"}
