"""S3 storage backend.

Uses :mod:`aioboto3` for fully-async S3 access. A single
``aioboto3.Session`` is constructed once per backend instance; each
operation opens a short-lived ``client`` context manager. The SDK handles
multipart split internally for large ``put_object`` calls — that is an
implementation detail of a single atomic :meth:`S3Backend.save` and is
not exposed to clients.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import aioboto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

from ..config import settings
from .base import StorageObject

_logger = logging.getLogger(settings.app_name)

# Stream-read chunk size for downloads. 64 KiB matches the
# botocore.response.StreamingBody internal buffer size, so larger values
# don't help and smaller values just add overhead.
_READ_CHUNK_SIZE = 64 * 1024


class S3Backend:
    """Store objects in an S3 (or S3-compatible) bucket.

    Configuration lives in :class:`fusionserve.config.S3Settings`.
    The backend resolves all options once at construction time; mutating
    :mod:`fusionserve.config` afterwards has no effect on this instance.
    """

    def __init__(self) -> None:
        """Snapshot the configured S3 settings."""
        cfg = settings.storage_s3
        if not cfg.bucket:
            # Defence-in-depth: ``Settings._validate_storage`` already
            # guarded this at startup, but instantiating the backend
            # directly in tests should still fail loudly.
            raise ValueError("S3Backend requires storage_s3.bucket to be set")
        self._bucket = cfg.bucket
        self._region = cfg.region
        self._endpoint_url = cfg.endpoint_url
        self._presign_ttl = cfg.presign_ttl_seconds
        secret = cfg.secret_access_key.get_secret_value() if cfg.secret_access_key else None
        self._session = aioboto3.Session(
            aws_access_key_id=cfg.access_key_id,
            aws_secret_access_key=secret,
            region_name=cfg.region,
        )
        # Disable the new (botocore 1.36+) default of computing
        # CRC32 checksums via aws-chunked encoding on every request:
        # many S3-compatible backends (MinIO, LocalStack older
        # versions, moto) do not understand the trailer-checksum
        # framing and respond with cryptic 5xx errors. The signed
        # ``x-amz-content-sha256`` header still protects integrity.
        self._client_config = BotoConfig(
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        )

    @property
    def bucket(self) -> str:
        """The configured bucket name."""
        return self._bucket

    def _client(self):
        """Open a short-lived async S3 client context manager."""
        return self._session.client(
            "s3",
            endpoint_url=self._endpoint_url,
            region_name=self._region,
            config=self._client_config,
        )

    async def save(
        self,
        key: str,
        stream: AsyncIterator[bytes],
        *,
        content_type: str,
        declared_size: int | None,
    ) -> StorageObject:
        """Upload ``stream`` to ``s3://<bucket>/<key>``.

        The body is fully buffered in memory before the PUT — the upload
        controller already enforces the per-file size cap, so the buffer
        is bounded by ``settings.storage_max_single_file_bytes``. This
        keeps the implementation simple and avoids running into S3's
        5 MiB minimum part size for multipart uploads, which clashes
        with small files.
        """
        del declared_size
        buffer = bytearray()
        async for chunk in stream:
            if chunk:
                buffer.extend(chunk)
        body = bytes(buffer)
        async with self._client() as client:
            response = await client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=body,
                ContentType=content_type,
            )
        etag = response.get("ETag", "").strip('"') or None
        return StorageObject(key=key, size_bytes=len(body), content_type=content_type, etag=etag)

    @asynccontextmanager
    async def open(self, key: str) -> AsyncIterator[AsyncIterator[bytes]]:
        """Stream ``key`` from S3 in chunks.

        Raises:
            FileNotFoundError: If the object does not exist (NoSuchKey).
        """
        async with self._client() as client:
            try:
                response = await client.get_object(Bucket=self._bucket, Key=key)
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code")
                if code in {"NoSuchKey", "404"}:
                    raise FileNotFoundError(key) from exc
                raise
            body = response["Body"]

            async def _iter() -> AsyncIterator[bytes]:
                try:
                    async for chunk in body.iter_chunks(_READ_CHUNK_SIZE):
                        yield chunk
                finally:
                    body.close()

            yield _iter()

    async def delete(self, key: str) -> None:
        """Idempotently delete the object.

        S3 ``DeleteObject`` returns success for non-existent keys, so no
        special-casing is needed.
        """
        async with self._client() as client:
            await client.delete_object(Bucket=self._bucket, Key=key)

    async def stat(self, key: str) -> StorageObject:
        """HEAD the object and return its metadata.

        Raises:
            FileNotFoundError: If the object does not exist.
        """
        async with self._client() as client:
            try:
                response = await client.head_object(Bucket=self._bucket, Key=key)
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code")
                if code in {"NoSuchKey", "404", "NotFound"}:
                    raise FileNotFoundError(key) from exc
                raise
        return StorageObject(
            key=key,
            size_bytes=int(response["ContentLength"]),
            content_type=response.get("ContentType", "application/octet-stream"),
            etag=(response.get("ETag") or "").strip('"') or None,
        )

    async def presigned_url(self, key: str, *, expires_in: int) -> str | None:
        """Return a presigned GET URL valid for ``expires_in`` seconds."""
        async with self._client() as client:
            url: str = await client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self._bucket, "Key": key},
                ExpiresIn=expires_in,
            )
        return url


__all__ = ["S3Backend"]
