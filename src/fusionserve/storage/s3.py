"""S3 storage backend.

Uses :mod:`aioboto3` for fully-async S3 access. A single
``aioboto3.Session`` is constructed once per backend instance; each
operation opens a short-lived ``client`` context manager.

The backend never streams object bytes through the application: it signs
a presigned ``PUT`` URL for uploads and a presigned ``GET`` URL for
downloads, and the client talks to S3 directly. :meth:`S3Backend.stat`
verifies an object after upload and :meth:`S3Backend.delete` removes it.
:meth:`S3Backend.object_origin` exposes the exact ``scheme://host`` the
presigned URLs use so the optional HTTP proxy can relay to it.
"""

from __future__ import annotations

import datetime
import logging
from urllib.parse import urlsplit

import aioboto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

from ..config import settings
from .base import StorageObject, UploadTicket

_logger = logging.getLogger(settings.app_name)

# Sentinel key used only to derive the presigned-URL origin locally
# (presigning is client-side crypto — no network call is made).
_ORIGIN_PROBE_KEY = "__fusionserve_origin_probe__"


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
            raise ValueError("S3Backend requires storage_s3.bucket to be set (STORAGE_S3__BUCKET)")
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
        self._origin: str | None = None

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

    async def generate_upload_url(self, key: str, *, content_type: str, expires_in: int) -> UploadTicket:
        """Return a presigned ``PUT`` URL for uploading ``key``.

        The ``ContentType`` is bound into the signature, so the client
        must send a matching ``Content-Type`` header — this keeps the
        stored object's MIME type truthful.
        """
        async with self._client() as client:
            url: str = await client.generate_presigned_url(
                "put_object",
                Params={"Bucket": self._bucket, "Key": key, "ContentType": content_type},
                ExpiresIn=expires_in,
            )
        expires_at = datetime.datetime.now(datetime.UTC) + datetime.timedelta(seconds=expires_in)
        _logger.debug("Generated presigned PUT URL for %r (expires %s)", key, expires_at)
        _logger.debug("Presigned URL: %s", url)
        return UploadTicket(url=url, method="PUT", headers={"Content-Type": content_type}, expires_at=expires_at)

    async def generate_download_url(self, key: str, *, expires_in: int) -> str:
        """Return a presigned ``GET`` URL valid for ``expires_in`` seconds."""
        async with self._client() as client:
            url: str = await client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self._bucket, "Key": key},
                ExpiresIn=expires_in,
            )
        return url

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

    async def delete(self, key: str) -> None:
        """Idempotently delete the object.

        S3 ``DeleteObject`` returns success for non-existent keys, so no
        special-casing is needed.
        """
        async with self._client() as client:
            await client.delete_object(Bucket=self._bucket, Key=key)

    async def object_origin(self) -> str:
        """Return the exact ``scheme://host`` presigned URLs point at.

        Derived by presigning a throwaway key and parsing the result,
        which guarantees the origin matches real presigned URLs
        regardless of addressing style (path vs virtual-hosted) or a
        custom ``endpoint_url`` (MinIO/LocalStack). Cached after the
        first call.
        """
        if self._origin is None:
            async with self._client() as client:
                probe: str = await client.generate_presigned_url(
                    "get_object",
                    Params={"Bucket": self._bucket, "Key": _ORIGIN_PROBE_KEY},
                    ExpiresIn=60,
                )
            parts = urlsplit(probe)
            self._origin = f"{parts.scheme}://{parts.netloc}"
        return self._origin


__all__ = ["S3Backend"]
