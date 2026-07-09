"""Azure Blob Storage backend — placeholder.

This reserves the ``"azure"`` backend selector and documents the intended
shape (SAS-based presigned uploads/downloads mirroring the S3 backend),
but is **not implemented**. Every method raises :class:`NotImplementedError`
so selecting it fails loudly at first use rather than silently
misbehaving. The class is instantiable with no arguments so
:func:`fusionserve.storage.load_backend` can still resolve and
protocol-check it.
"""

from __future__ import annotations

from .base import StorageObject, UploadTicket

_NOT_IMPLEMENTED = "Azure Blob Storage backend is not yet implemented"


class AzureBlobBackend:
    """Placeholder backend for Azure Blob Storage.

    Intended to issue a Shared Access Signature (SAS) for direct client
    uploads/downloads, analogous to :class:`fusionserve.storage.s3.S3Backend`.
    Not implemented yet.
    """

    async def generate_upload_url(self, key: str, *, content_type: str, expires_in: int) -> UploadTicket:
        """Not implemented."""
        raise NotImplementedError(_NOT_IMPLEMENTED)

    async def generate_download_url(self, key: str, *, expires_in: int) -> str:
        """Not implemented."""
        raise NotImplementedError(_NOT_IMPLEMENTED)

    async def stat(self, key: str) -> StorageObject:
        """Not implemented."""
        raise NotImplementedError(_NOT_IMPLEMENTED)

    async def delete(self, key: str) -> None:
        """Not implemented."""
        raise NotImplementedError(_NOT_IMPLEMENTED)

    async def object_origin(self) -> str:
        """Not implemented."""
        raise NotImplementedError(_NOT_IMPLEMENTED)


__all__ = ["AzureBlobBackend"]
