"""Litestar controller for the file-uploads feature.

Mounted at ``<base_path>/v1/_uploads``. The leading underscore namespaces
the controller's routes away from the auto-generated CRUD for the
``uploads`` table (which lives at ``<base_path>/v1/uploads``). The two
coexist by design: the auto-generated CRUD exposes the metadata rows,
and this controller adds the blob-aware operations (multi-file upload,
streamed download, cascading delete).
"""

from __future__ import annotations

import datetime
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Annotated, ClassVar

import litestar
from litestar import Request
from litestar.datastructures import State, UploadFile
from litestar.enums import RequestEncodingType
from litestar.exceptions import ClientException, NotAuthorizedException, NotFoundException
from litestar.params import Body
from litestar.response import Redirect, Stream
from litestar.status_codes import HTTP_207_MULTI_STATUS
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import DeclarativeMeta

from .. import auth
from ..config import settings
from ..persistence import set_role
from ..storage import StorageBackend
from .keys import make_storage_key

_logger = logging.getLogger(settings.app_name)

# Chunk size used when relaying the request body to the storage backend.
_UPLOAD_CHUNK_SIZE = 64 * 1024


class UploadModel(BaseModel):
    """Pydantic projection of one row from the ``uploads`` table.

    Mirrors the columns enforced by
    :func:`fusionserve.files.metadata.validate_uploads_table`.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    filename: str
    content_type: str
    size_bytes: int
    storage_key: str
    storage_backend: str
    uploaded_by: uuid.UUID | None = None
    uploaded_at: datetime.datetime


class UploadItem(BaseModel):
    """One entry in the multi-file upload response.

    Attributes:
        status: ``"ok"`` when the file was persisted, ``"error"`` when it
            was rejected (e.g. exceeded the per-file size cap). The
            request as a whole still returns 207 in the latter case so
            partially-successful batches can be inspected by the client.
        filename: Original client-supplied filename (always populated so
            clients can correlate failed parts).
        upload: Populated when ``status == "ok"``.
        error: Populated when ``status == "error"``.
    """

    model_config = ConfigDict(from_attributes=True)

    status: str
    filename: str
    upload: UploadModel | None = None
    error: str | None = None


class UploadBatchResponse(BaseModel):
    """Wrapper returned by ``POST /api/v1/_uploads``.

    Always carries an ``items`` array, even for single-file uploads,
    keeping the response shape predictable for clients.
    """

    items: list[UploadItem] = Field(default_factory=list)


async def _stream_to_backend(
    file: UploadFile,
    *,
    storage: StorageBackend,
    key: str,
    max_bytes: int,
) -> int:
    """Pipe ``file`` into ``storage.save``, enforcing the per-file cap.

    Returns the number of bytes written. Raises :class:`ClientException`
    with HTTP 413 semantics when the file exceeds ``max_bytes``.
    """
    total = 0
    oversize = False

    async def _iter() -> AsyncIterator[bytes]:
        nonlocal total, oversize
        while True:
            chunk = await file.read(_UPLOAD_CHUNK_SIZE)
            if not chunk:
                return
            total += len(chunk)
            if total > max_bytes:
                oversize = True
                return
            yield chunk

    obj = await storage.save(
        key,
        _iter(),
        content_type=file.content_type or "application/octet-stream",
        declared_size=None,
    )
    if oversize:
        # Clean up partial upload — best-effort.
        try:
            await storage.delete(key)
        except Exception:
            _logger.warning("Failed to delete partial upload at %s after size-limit breach", key, exc_info=True)
        raise ClientException(
            status_code=413,
            detail=f"file {file.filename!r} exceeds per-file size limit of {max_bytes} bytes",
        )
    return obj.size_bytes


def build_controller(
    orm_class: DeclarativeMeta,
    storage: StorageBackend,
) -> type[litestar.Controller]:
    """Build the files controller bound to ``orm_class`` and ``storage``.

    Args:
        orm_class: The automap-generated ORM class for the configured
            metadata table. Already validated by
            :func:`fusionserve.files.metadata.validate_uploads_table`.
        storage: The configured storage backend.

    Returns:
        A Litestar :class:`~litestar.Controller` subclass ready to be
        registered with the application.
    """
    backend_name = type(storage).__name__
    max_single_file = settings.storage_max_single_file_bytes
    presign_ttl = settings.storage_s3.presign_ttl_seconds

    class FilesController(litestar.Controller):
        """Auto-built controller for multi-file uploads and downloads."""

        path = f"{settings.base_path}/v1/_uploads"
        tags: ClassVar[list[str]] = ["files: uploads"]

        @litestar.post(
            summary="Upload one or more files",
            description=(
                "Upload one or more files via ``multipart/form-data``. "
                "Each file part becomes a row in the uploads metadata "
                "table and a blob in the configured storage backend. "
                "Per-file errors do not abort the batch — inspect "
                "``items[*].status``."
            ),
            security=[{"BearerToken": []}],
            status_code=HTTP_207_MULTI_STATUS,
        )
        async def upload(
            self,
            session: AsyncSession,
            request: Request[auth.User, str, State],
            data: Annotated[list[UploadFile], Body(media_type=RequestEncodingType.MULTI_PART)],
        ) -> UploadBatchResponse:
            """Persist each part to storage and metadata."""
            if request.user is None:
                raise NotAuthorizedException("Authentication required to upload files")
            if not data:
                raise ClientException("no files in request")
            await set_role(session, request.user)
            items: list[UploadItem] = []
            for file in data:
                filename = file.filename or "unnamed"
                content_type = file.content_type or "application/octet-stream"
                key = make_storage_key(content_type)
                try:
                    size_bytes = await _stream_to_backend(
                        file,
                        storage=storage,
                        key=key,
                        max_bytes=max_single_file,
                    )
                except ClientException as exc:
                    items.append(UploadItem(status="error", filename=filename, error=str(exc.detail)))
                    continue
                except Exception as exc:
                    _logger.exception("Storage backend failed for %r", filename)
                    items.append(UploadItem(status="error", filename=filename, error=f"storage error: {exc}"))
                    continue

                row = orm_class(
                    filename=filename,
                    content_type=content_type,
                    size_bytes=size_bytes,
                    storage_key=key,
                    storage_backend=backend_name,
                    uploaded_by=request.user.id,
                )
                session.add(row)
                try:
                    await session.flush()
                except Exception as exc:
                    _logger.exception("Failed to record upload metadata for %r", filename)
                    await session.rollback()
                    # Reapply role since rollback resets per-session config.
                    await set_role(session, request.user)
                    try:
                        await storage.delete(key)
                    except Exception:
                        _logger.warning("Failed to delete orphaned blob %s after DB error", key, exc_info=True)
                    items.append(UploadItem(status="error", filename=filename, error=f"database error: {exc}"))
                    continue
                items.append(UploadItem(status="ok", filename=filename, upload=UploadModel.model_validate(row)))
            await session.commit()
            return UploadBatchResponse(items=items)

        @litestar.get(
            path="/{id:uuid}/content",
            summary="Download a previously-uploaded file",
            description=(
                "Stream the file's bytes through the application by "
                "default. Pass ``?redirect=1`` to receive a 302 to a "
                "backend-issued presigned URL when the backend supports "
                "it; backends without presigning (e.g. filesystem) "
                "fall back to streaming."
            ),
            security=[{"BearerToken": []}],
            raises=[NotFoundException],
        )
        async def download(
            self,
            session: AsyncSession,
            request: Request[auth.User, str, State],
            id: uuid.UUID,
            redirect: bool = False,
        ) -> Stream | Redirect:
            """Return the file contents (stream) or a presigned redirect."""
            await set_role(session, request.user)
            row = await session.get(orm_class, {"id": id})
            if row is None:
                raise NotFoundException(f"No upload with id {id}")
            key = row.storage_key
            content_type = row.content_type
            filename = row.filename

            if redirect:
                url = await storage.presigned_url(key, expires_in=presign_ttl)
                if url is not None:
                    return Redirect(path=url)

            try:
                stat = await storage.stat(key)
            except FileNotFoundError as exc:
                raise NotFoundException(f"Blob for upload {id} not found in backend") from exc

            async def _body() -> AsyncIterator[bytes]:
                async with storage.open(key) as chunks:
                    async for chunk in chunks:
                        yield chunk

            headers = {
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Content-Length": str(stat.size_bytes),
            }
            return Stream(_body(), media_type=content_type, headers=headers)

        @litestar.delete(
            path="/{id:uuid}",
            summary="Delete an uploaded file (cascading)",
            description=(
                "Delete the blob from the storage backend *first*, then "
                "the metadata row. This is the safe deletion path; the "
                "auto-generated ``DELETE /api/v1/uploads/{id}`` only "
                "removes the metadata row and orphans the blob."
            ),
            security=[{"BearerToken": []}],
            raises=[NotFoundException],
        )
        async def remove(
            self,
            session: AsyncSession,
            request: Request[auth.User, str, State],
            id: uuid.UUID,
        ) -> None:
            """Cascading delete: backend object first, then metadata row."""
            if request.user is None:
                raise NotAuthorizedException("Authentication required to delete files")
            await set_role(session, request.user)
            row = await session.get(orm_class, {"id": id})
            if row is None:
                raise NotFoundException(f"No upload with id {id}")
            key = row.storage_key
            await storage.delete(key)
            await session.delete(row)
            await session.commit()

    return FilesController


__all__ = ["UploadBatchResponse", "UploadItem", "UploadModel", "build_controller"]
