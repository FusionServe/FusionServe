"""File-upload feature package.

The public surface is :func:`build_controller`, used by
:mod:`fusionserve.main` at lifespan startup to mount the files routes
when the operator-supplied ``uploads`` table is present.
"""

from __future__ import annotations

from .controller import (
    CompleteUploadRequest,
    InitBatchResponse,
    InitUploadFile,
    InitUploadRequest,
    UploadModel,
    UploadTicketItem,
    build_controller,
)
from .metadata import validate_uploads_table

__all__ = [
    "CompleteUploadRequest",
    "InitBatchResponse",
    "InitUploadFile",
    "InitUploadRequest",
    "UploadModel",
    "UploadTicketItem",
    "build_controller",
    "validate_uploads_table",
]
