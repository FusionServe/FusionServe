"""Startup-time validation of the operator-supplied ``uploads`` table.

The files controller assumes a specific column set; introspecting and
validating that contract once at lifespan time keeps the handler code
free of defensive ``getattr`` and produces a clear error message at
startup when the operator hasn't created the table correctly.
"""

from __future__ import annotations

import datetime
import logging
import uuid

from sqlalchemy import Table
from sqlalchemy.orm import DeclarativeMeta

from ..config import settings

_logger = logging.getLogger(settings.app_name)


#: Required column → Python type. Nullability is enforced separately
#: below (only ``uploaded_by`` is allowed to be nullable).
_REQUIRED_COLUMNS: dict[str, type] = {
    "id": uuid.UUID,
    "filename": str,
    "content_type": str,
    "size_bytes": int,
    "storage_key": str,
    "storage_backend": str,
    "uploaded_by": uuid.UUID,
    "uploaded_at": datetime.datetime,
}

#: Columns that are allowed to carry NULL.
_NULLABLE_ALLOWED = frozenset({"uploaded_by"})


def validate_uploads_table(orm_class: DeclarativeMeta) -> None:
    """Verify the ``uploads`` table matches the documented contract.

    Args:
        orm_class: The automap ORM class produced by introspection for
            the configured metadata table.

    Raises:
        ValueError: If a required column is missing, has the wrong
            Python type, or carries an unexpected nullability.
    """
    table: Table = orm_class.__table__  # type: ignore[attr-defined]
    table_name = table.name
    missing = sorted(set(_REQUIRED_COLUMNS) - set(table.columns.keys()))
    if missing:
        raise ValueError(
            f"Table {settings.pg_app_schema}.{table_name} is missing required columns: {missing}. "
            "See docs/files.md for the expected DDL."
        )
    for name, expected_type in _REQUIRED_COLUMNS.items():
        column = table.columns[name]
        try:
            actual_type = column.type.python_type
        except NotImplementedError as exc:
            raise ValueError(
                f"Column {table_name}.{name} has a SQLAlchemy type without a Python mapping; "
                f"expected {expected_type.__name__}."
            ) from exc
        if actual_type is not expected_type:
            raise ValueError(
                f"Column {table_name}.{name} has Python type {actual_type.__name__}; expected {expected_type.__name__}."
            )
        if column.nullable and name not in _NULLABLE_ALLOWED:
            raise ValueError(
                f"Column {table_name}.{name} is nullable; only {sorted(_NULLABLE_ALLOWED)} may be nullable."
            )
    _logger.debug("Validated metadata table %s.%s", settings.pg_app_schema, table_name)
