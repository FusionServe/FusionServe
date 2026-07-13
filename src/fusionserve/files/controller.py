"""Litestar controller for the file-uploads feature.

Mounted at ``<base_path>/v1/_uploads``. The leading underscore namespaces
the controller's routes away from the auto-generated CRUD for the
``uploads`` table (which lives at ``<base_path>/v1/uploads``). The two
coexist by design: the auto-generated CRUD exposes the metadata rows,
and this controller adds the blob-aware operations.

Uploads are **direct-to-store** and two-phase:

1. ``POST /_uploads`` (*init*) — creates a ``pending`` metadata row per
   file and returns a presigned upload URL (an :class:`UploadTicket`).
2. the client uploads the bytes straight to the object store using that
   URL (or through the proxy — see below).
3. ``POST /_uploads/{id}/complete`` — the server HEADs the object,
   enforces the size cap, records the verified size/etag and flips the
   row to ``completed``.

Downloads issue a 302 to a presigned GET URL. When
``settings.storage_proxy_urls`` is on, both the upload and download URLs
are rewritten to point at this controller's ``proxy`` relay, so clients
never contact the object store directly.
"""

from __future__ import annotations

import datetime
import logging
import re
import uuid
from collections.abc import AsyncIterator
from typing import Any, ClassVar
from urllib.parse import urlsplit

import httpx
import litestar
from litestar import Request, Response
from litestar.datastructures import State
from litestar.exceptions import ClientException, NotAuthorizedException, NotFoundException
from litestar.response import Redirect, Stream
from litestar.status_codes import HTTP_201_CREATED
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import DeclarativeMeta

from .. import auth
from ..config import settings
from ..persistence import set_role
from ..storage import StorageBackend

_logger = logging.getLogger(settings.app_name)

# Chunk size used when relaying bytes to/from the object store.
_RELAY_CHUNK_SIZE = 64 * 1024


class UploadModel(BaseModel):
    """Pydantic projection of one row from the ``uploads`` table.

    Mirrors the columns enforced by
    :func:`fusionserve.files.metadata.validate_uploads_table`.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    filename: str
    content_type: str
    size_bytes: int | None = None
    storage_key: str
    storage_backend: str
    status: str
    etag: str | None = None
    attributes: dict[str, Any] | None = None
    uploaded_by: uuid.UUID | None = None
    uploaded_at: datetime.datetime


class InitUploadFile(BaseModel):
    """One file descriptor in an upload-init request.

    Attributes:
        filename: Client filename, used as the storage key after
            sanitizing: directory structure is preserved, control
            characters and any leading ``./`` are stripped, and runs of
            two or more dots are collapsed to one so ``..`` traversal is
            neutralized. Keys form a global namespace — a duplicate is
            rejected with 409 (the metadata table enforces
            ``UNIQUE(storage_key)``).
        content_type: MIME type the object will be stored with. Bound
            into the presigned URL, so the client must upload with a
            matching ``Content-Type`` header.
        attributes: Optional arbitrary JSON metadata stored alongside the
            row. May be overridden at complete time.
    """

    filename: str
    content_type: str = "application/octet-stream"
    attributes: dict[str, Any] | None = None


class InitUploadRequest(BaseModel):
    """Body of ``POST /_uploads``: one or more files to authorize."""

    files: list[InitUploadFile] = Field(default_factory=list)


class UploadTicketItem(BaseModel):
    """One entry in the init response: a pending row plus its upload URL."""

    id: uuid.UUID
    filename: str
    storage_key: str
    upload_url: str
    method: str
    headers: dict[str, str]
    expires_at: datetime.datetime


class InitBatchResponse(BaseModel):
    """Wrapper returned by ``POST /_uploads``.

    Always carries an ``items`` array, even for single-file requests,
    keeping the response shape predictable for clients.
    """

    items: list[UploadTicketItem] = Field(default_factory=list)


class CompleteUploadRequest(BaseModel):
    """Optional body of ``POST /_uploads/{id}/complete``.

    Attributes:
        attributes: When provided, overwrites the row's ``attributes``
            JSONB bag. Omit (or send no body) to keep the init-time value.
    """

    attributes: dict[str, Any] | None = None


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

    def _maybe_proxy(url: str, request: Request[Any, Any, Any]) -> str:
        """Origin-swap ``url`` onto the ``proxy`` relay when proxying is on.

        The object store's ``scheme://host`` is replaced with FusionServe's
        own base plus the relay prefix, while the path and query (which
        carry the presigned signature) are preserved verbatim.
        """
        if not settings.storage_proxy_urls:
            return url
        parts = urlsplit(url)
        base = str(request.base_url).rstrip("/")
        proxied = f"{base}{settings.base_path}/v1/_uploads/proxy{parts.path}"
        return f"{proxied}?{parts.query}" if parts.query else proxied

    class FilesController(litestar.Controller):
        """Auto-built controller for direct-to-store uploads and downloads."""

        path = f"{settings.base_path}/v1/_uploads"
        tags: ClassVar[list[str]] = ["files: uploads"]

        @litestar.post(
            summary="Initiate one or more direct uploads",
            description=(
                "Create a ``pending`` metadata row for each file and "
                "return a presigned upload URL. The client uploads the "
                "bytes directly to the returned ``upload_url`` (sending "
                "the returned ``headers``), then calls "
                "``POST /_uploads/{id}/complete``."
            ),
            security=[{"BearerToken": []}],
            status_code=HTTP_201_CREATED,
        )
        async def init(
            self,
            session: AsyncSession,
            request: Request[auth.User, str, State],
            data: InitUploadRequest,
        ) -> InitBatchResponse:
            """Sign an upload URL and persist a pending row per file."""
            if request.user is None:
                raise NotAuthorizedException("Authentication required to upload files")
            if not data.files:
                raise ClientException("no files in request")
            await set_role(session, request.user)
            items: list[UploadTicketItem] = []
            for file in data.files:
                content_type = file.content_type or "application/octet-stream"
                # The storage key is the client filename, sanitized: directory
                # structure is preserved (``/`` kept), control characters are
                # stripped (spaces/unicode kept), any leading ``./`` is removed,
                # and runs of 2+ dots are collapsed to a single dot so ``..``
                # path traversal cannot survive anywhere in the path.
                # ``UNIQUE(storage_key)`` enforces the 409 below.
                filename = file.filename.replace("\\", "/")
                cleaned = "".join(ch for ch in filename if ch.isprintable()).strip().lstrip("./")
                key = re.sub(r"\.{2,}", ".", cleaned)[:200]
                ticket = await storage.generate_upload_url(key, content_type=content_type, expires_in=presign_ttl)
                row = orm_class(
                    filename=file.filename,
                    content_type=content_type,
                    size_bytes=None,
                    storage_key=key,
                    storage_backend=backend_name,
                    status="pending",
                    attributes=file.attributes,
                    uploaded_by=request.user.id,
                )
                session.add(row)
                try:
                    await session.flush()
                except IntegrityError as exc:
                    await session.rollback()
                    raise ClientException(
                        status_code=409,
                        detail=f"an upload named {key!r} already exists",
                    ) from exc
                items.append(
                    UploadTicketItem(
                        id=row.id,
                        filename=file.filename,
                        storage_key=key,
                        upload_url=_maybe_proxy(ticket.url, request),
                        method=ticket.method,
                        headers=ticket.headers,
                        expires_at=ticket.expires_at,
                    )
                )
            await session.commit()
            return InitBatchResponse(items=items)

        @litestar.post(
            path="/{id:uuid}/complete",
            summary="Finalize a previously-initiated upload",
            description=(
                "Verify the object exists in the backend, enforce the "
                "per-file size limit, record the verified size/etag and "
                "mark the row ``completed``. Optionally overwrite the "
                "``attributes`` JSONB bag."
            ),
            security=[{"BearerToken": []}],
            raises=[NotFoundException],
        )
        async def complete(
            self,
            session: AsyncSession,
            request: Request[auth.User, str, State],
            id: uuid.UUID,
            data: CompleteUploadRequest | None = None,
        ) -> UploadModel:
            """HEAD-verify the object and flip the row to ``completed``."""
            if request.user is None:
                raise NotAuthorizedException("Authentication required to complete uploads")
            await set_role(session, request.user)
            row = await session.get(orm_class, {"id": id})
            if row is None:
                raise NotFoundException(f"No upload with id {id}")
            try:
                stat = await storage.stat(row.storage_key)
            except FileNotFoundError as exc:
                raise ClientException(
                    status_code=409,
                    detail="object has not been uploaded to storage yet",
                ) from exc
            if stat.size_bytes > max_single_file:
                await storage.delete(row.storage_key)
                await session.delete(row)
                await session.commit()
                raise ClientException(
                    status_code=413,
                    detail=f"uploaded object exceeds per-file size limit of {max_single_file} bytes",
                )
            row.size_bytes = stat.size_bytes
            row.etag = stat.etag
            row.status = "completed"
            if data is not None and data.attributes is not None:
                row.attributes = data.attributes
            await session.commit()
            return UploadModel.model_validate(row)

        @litestar.get(
            path="/{id:uuid}/content",
            summary="Download a previously-uploaded file",
            description=(
                "Return a 302 redirect to a presigned GET URL. When "
                "URL-proxying is enabled the redirect points at the "
                "``proxy`` relay instead of the object store."
            ),
            security=[{"BearerToken": []}],
            raises=[NotFoundException],
        )
        async def download(
            self,
            session: AsyncSession,
            request: Request[auth.User, str, State],
            id: uuid.UUID,
        ) -> Redirect:
            """Redirect to a presigned (or proxied) download URL."""
            if request.user is None:
                raise NotAuthorizedException("Authentication required to download files")
            await set_role(session, request.user)
            row = await session.get(orm_class, {"id": id})
            if row is None:
                raise NotFoundException(f"No upload with id {id}")
            url = await storage.generate_download_url(row.storage_key, expires_in=presign_ttl)
            return Redirect(path=_maybe_proxy(url, request), status_code=302)

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

        # ---- Optional HTTP proxy relay ---------------------------------
        # These routes reconstruct the object-store URL from the backend's
        # own origin (never from client input) and relay the request. They
        # are unauthenticated: the presigned signature in the query string
        # is the capability, exactly like a raw presigned URL.

        @litestar.put(
            path="/proxy/{s3path:path}",
            summary="Relay a direct upload to the object store",
            include_in_schema=False,
            opt={"exclude_from_auth": True},
            request_max_body_size=max_single_file,
        )
        async def proxy_upload(
            self,
            request: Request[Any, Any, State],
            s3path: str,
        ) -> Response[bytes]:
            """Stream the request body to the presigned object-store URL."""
            origin = await storage.object_origin()
            target = f"{origin.rstrip('/')}/{s3path.lstrip('/')}"
            if request.url.query:
                target = f"{target}?{request.url.query}"
            headers: dict[str, str] = {}
            content_type = request.headers.get("content-type")
            if content_type:
                headers["Content-Type"] = content_type

            async def _body() -> AsyncIterator[bytes]:
                async for chunk in request.stream():
                    if chunk:
                        yield chunk

            async with httpx.AsyncClient(timeout=None) as client:
                upstream = await client.request("PUT", target, content=_body(), headers=headers)
            resp_headers = {}
            if "etag" in upstream.headers:
                resp_headers["ETag"] = upstream.headers["etag"]
            return Response(content=b"", status_code=upstream.status_code, headers=resp_headers)

        @litestar.get(
            path="/proxy/{s3path:path}",
            summary="Relay a download from the object store",
            include_in_schema=False,
            opt={"exclude_from_auth": True},
        )
        async def proxy_download(
            self,
            request: Request[Any, Any, State],
            s3path: str,
        ) -> Stream:
            """Stream the object-store response back to the client."""
            origin = await storage.object_origin()
            target = f"{origin.rstrip('/')}/{s3path.lstrip('/')}"
            if request.url.query:
                target = f"{target}?{request.url.query}"
            client = httpx.AsyncClient(timeout=None)
            upstream = await client.send(client.build_request("GET", target), stream=True)
            if upstream.status_code >= 400:
                await upstream.aread()
                await upstream.aclose()
                await client.aclose()
                raise NotFoundException("object not found")

            forwarded = {}
            for header in ("content-length", "etag", "content-disposition"):
                if header in upstream.headers:
                    forwarded[header] = upstream.headers[header]
            media_type = upstream.headers.get("content-type", "application/octet-stream")

            async def _body() -> AsyncIterator[bytes]:
                try:
                    async for chunk in upstream.aiter_bytes(_RELAY_CHUNK_SIZE):
                        yield chunk
                finally:
                    await upstream.aclose()
                    await client.aclose()

            return Stream(_body(), media_type=media_type, headers=forwarded)

    return FilesController


__all__ = [
    "CompleteUploadRequest",
    "InitBatchResponse",
    "InitUploadFile",
    "InitUploadRequest",
    "UploadModel",
    "UploadTicketItem",
    "build_controller",
]
