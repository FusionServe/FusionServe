"""Tests for :func:`fusionserve.files.metadata.validate_uploads_table`.

The validator is the only safeguard between a malformed
operator-supplied table and the runtime handlers; covering every
rejection branch keeps regressions noisy.
"""

from __future__ import annotations

import pytest
from sqlalchemy import BigInteger, Column, DateTime, MetaData, String, Table
from sqlalchemy.dialects.postgresql import JSONB, UUID

from fusionserve.files.metadata import validate_uploads_table


def _make_table(*, drop: str | None = None, change: dict[str, Column] | None = None) -> type:
    """Build an ``uploads`` table; optionally drop or replace columns."""
    columns = {
        "id": Column("id", UUID(as_uuid=True), primary_key=True),
        "filename": Column("filename", String, nullable=False),
        "content_type": Column("content_type", String, nullable=False),
        "size_bytes": Column("size_bytes", BigInteger, nullable=True),
        "storage_key": Column("storage_key", String, unique=True, nullable=False),
        "storage_backend": Column("storage_backend", String, nullable=False),
        "status": Column("status", String, nullable=False),
        "etag": Column("etag", String, nullable=True),
        "attributes": Column("attributes", JSONB, nullable=True),
        "uploaded_by": Column("uploaded_by", UUID(as_uuid=True), nullable=True),
        "uploaded_at": Column("uploaded_at", DateTime(timezone=True), nullable=False),
    }
    if change:
        columns.update(change)
    if drop:
        columns.pop(drop)
    metadata = MetaData()
    table = Table("uploads", metadata, *columns.values())

    class _Stub:
        __table__ = table

    return _Stub


def test_validate_uploads_table_accepts_documented_schema():
    """The documented DDL must validate cleanly."""
    validate_uploads_table(_make_table())


def test_validate_uploads_table_rejects_missing_column():
    """A missing required column must raise ``ValueError``."""
    with pytest.raises(ValueError, match="missing required columns"):
        validate_uploads_table(_make_table(drop="storage_key"))


def test_validate_uploads_table_rejects_wrong_type():
    """A column with the wrong Python type must raise ``ValueError``."""
    bad = _make_table(change={"size_bytes": Column("size_bytes", String, nullable=False)})
    with pytest.raises(ValueError, match="size_bytes"):
        validate_uploads_table(bad)


def test_validate_uploads_table_rejects_unexpected_nullable():
    """Non-allowlisted columns must not be nullable."""
    bad = _make_table(change={"filename": Column("filename", String, nullable=True)})
    with pytest.raises(ValueError, match="filename"):
        validate_uploads_table(bad)


def test_validate_uploads_table_allows_nullable_uploaded_by():
    """``uploaded_by`` is allowed (and recommended) to be nullable."""
    # The default fixture already has uploaded_by nullable; just confirm it passes.
    validate_uploads_table(_make_table())


def test_validate_uploads_table_allows_nullable_size_etag_attributes():
    """``size_bytes``/``etag``/``attributes`` are unknown until complete."""
    validate_uploads_table(
        _make_table(
            change={
                "size_bytes": Column("size_bytes", BigInteger, nullable=True),
                "etag": Column("etag", String, nullable=True),
                "attributes": Column("attributes", JSONB, nullable=True),
            }
        )
    )


def test_validate_uploads_table_rejects_missing_status():
    """The new ``status`` column is required."""
    with pytest.raises(ValueError, match="missing required columns"):
        validate_uploads_table(_make_table(drop="status"))


def test_validate_uploads_table_rejects_wrong_attributes_type():
    """``attributes`` must map to a JSON (dict) Python type."""
    bad = _make_table(change={"attributes": Column("attributes", String, nullable=True)})
    with pytest.raises(ValueError, match="attributes"):
        validate_uploads_table(bad)
