"""Local-filesystem storage backend.

Persists objects under :attr:`fusionserve.config.Settings.storage_fs_root`.
Writes are streamed through :mod:`aiofiles` to a ``.partial`` sibling and
then atomically renamed into place, so partial files are never visible
under their final key.
"""

from __future__ import annotations

import logging
import mimetypes
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import aiofiles

from ..config import settings
from .base import StorageObject

_logger = logging.getLogger(settings.app_name)

# Chunk size for streamed downloads. 64 KiB matches the typical
# filesystem readahead window; larger chunks add latency to the first
# byte without measurably improving throughput.
_READ_CHUNK_SIZE = 64 * 1024


class FilesystemBackend:
    """Store objects on the local filesystem under ``storage_fs_root``.

    The backend is stateless apart from the configured root directory; it
    is safe to share across the application lifetime.
    """

    def __init__(self, root: Path | None = None) -> None:
        """Initialise the backend.

        Args:
            root: Optional override for the storage root. Defaults to
                ``settings.storage_fs_root``. The directory is created
                lazily on first write.
        """
        self._root = (root or settings.storage_fs_root).expanduser().resolve()

    @property
    def root(self) -> Path:
        """The configured root directory (absolute, resolved)."""
        return self._root

    def _path_for(self, key: str) -> Path:
        """Return the absolute on-disk path for ``key``.

        Resolves the joined path and confirms it stays under ``root`` to
        defend against path-traversal in callers — keys are always
        server-generated so this should never trip, but defence in depth
        is cheap.
        """
        candidate = (self._root / key).resolve()
        try:
            candidate.relative_to(self._root)
        except ValueError as exc:
            raise ValueError(f"key {key!r} escapes storage root") from exc
        return candidate

    async def save(
        self,
        key: str,
        stream: AsyncIterator[bytes],
        *,
        content_type: str,
        declared_size: int | None,
    ) -> StorageObject:
        """Stream ``stream`` to ``<root>/<key>`` atomically.

        The bytes are written to a ``.partial`` sibling first; on
        successful drain the temp file is renamed into place via
        :func:`os.replace`, which is atomic on POSIX.
        """
        del declared_size  # not used; controller enforces size limit upstream
        target = self._path_for(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        # Unique partial name in case two writers race on the same key.
        tmp = target.with_name(f"{target.name}.{uuid.uuid4().hex}.partial")
        written = 0
        try:
            async with aiofiles.open(tmp, "wb") as fh:
                async for chunk in stream:
                    if not chunk:
                        continue
                    await fh.write(chunk)
                    written += len(chunk)
            os.replace(tmp, target)
        except BaseException:
            # Best-effort cleanup of the partial file on any failure
            # (including cancellation).
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                _logger.warning("Failed to unlink partial file %s", tmp, exc_info=True)
            raise
        return StorageObject(key=key, size_bytes=written, content_type=content_type, etag=None)

    @asynccontextmanager
    async def open(self, key: str) -> AsyncIterator[AsyncIterator[bytes]]:
        """Yield an async iterator of byte chunks for ``key``.

        Raises:
            FileNotFoundError: If the key does not exist.
        """
        path = self._path_for(key)
        if not path.exists():
            raise FileNotFoundError(key)

        async def _iter() -> AsyncIterator[bytes]:
            async with aiofiles.open(path, "rb") as fh:
                while True:
                    chunk = await fh.read(_READ_CHUNK_SIZE)
                    if not chunk:
                        break
                    yield chunk

        yield _iter()

    async def delete(self, key: str) -> None:
        """Idempotently remove ``key`` from disk."""
        path = self._path_for(key)
        try:
            path.unlink(missing_ok=True)
        except IsADirectoryError as exc:
            raise OSError(f"refusing to delete directory at {key!r}") from exc

    async def stat(self, key: str) -> StorageObject:
        """Return :class:`StorageObject` metadata for ``key``."""
        path = self._path_for(key)
        if not path.exists():
            raise FileNotFoundError(key)
        size = path.stat().st_size
        guessed, _ = mimetypes.guess_type(path.name)
        return StorageObject(
            key=key,
            size_bytes=size,
            content_type=guessed or "application/octet-stream",
            etag=None,
        )

    async def presigned_url(self, key: str, *, expires_in: int) -> str | None:
        """Filesystem objects are not directly URL-accessible.

        Always returns ``None``; the controller falls back to streaming
        the bytes through the application.
        """
        del key, expires_in
        return None
