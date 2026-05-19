"""Server-side key naming for uploaded objects.

Keys are always server-generated to keep the metadata table and the
storage backend in lock-step: clients never get to choose the path under
which their bytes end up. The format is
``{yyyy}/{mm}/{dd}/{uuid4}{ext}`` where ``ext`` derives from the
validated MIME type via :func:`mimetypes.guess_extension`.
"""

from __future__ import annotations

import datetime
import mimetypes
import uuid


def make_storage_key(content_type: str, *, now: datetime.datetime | None = None) -> str:
    """Return a fresh, server-controlled storage key.

    Args:
        content_type: The validated MIME type of the upload. Used to
            pick a sensible extension; the path itself never echoes the
            client filename.
        now: Optional override for the current time (testing).

    Returns:
        A relative storage key of the form ``YYYY/MM/DD/<uuid4><ext>``.
    """
    timestamp = now or datetime.datetime.now(datetime.UTC)
    extension = mimetypes.guess_extension(content_type) or ".bin"
    return f"{timestamp:%Y/%m/%d}/{uuid.uuid4().hex}{extension}"
