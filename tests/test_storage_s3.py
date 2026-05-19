"""Contract-level tests for :class:`fusionserve.storage.s3.S3Backend`.

These tests mock the aioboto3 client layer instead of standing up a
moto-backed S3. Rationale:

* aiobotocore (≥ 2.25) and moto don't share an HTTP-stubbing contract,
  so the round-trip path is brittle across patch releases.
* The S3 backend itself is a thin adapter — the value of testing it is
  pinning the wire-level operations it invokes (``put_object``,
  ``get_object``, ``head_object``, ``delete_object``,
  ``generate_presigned_url``), not re-testing S3 semantics.
* The "mock the boundary" pattern matches the rest of the FusionServe
  unit suite (see AGENTS.md → Tests).

Integration coverage against a live MinIO or real S3 is out of scope of
the unit suite and lives behind ``RUN_INTEGRATION=1`` in
``test_files_integration.py`` (added when the feature graduates from
alpha).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from botocore.exceptions import ClientError

from fusionserve.config import settings
from fusionserve.storage.s3 import S3Backend

_BUCKET = "fusionserve-test"


async def _async_iter(chunks: list[bytes]) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk


@pytest.fixture
def s3_settings(monkeypatch):
    monkeypatch.setattr(settings.storage_s3, "bucket", _BUCKET)
    monkeypatch.setattr(settings.storage_s3, "region", "us-east-1")
    monkeypatch.setattr(settings.storage_s3, "endpoint_url", None)


def _install_client(backend: S3Backend, client: MagicMock) -> None:
    """Replace ``backend._client`` with a context manager yielding ``client``."""

    @asynccontextmanager
    async def _cm():
        yield client

    backend._client = _cm  # type: ignore[assignment]


def _client_error(code: str) -> ClientError:
    return ClientError(
        error_response={"Error": {"Code": code, "Message": code}},
        operation_name="op",
    )


async def test_save_calls_put_object_with_body_and_content_type(s3_settings):
    """``save`` must invoke ``put_object`` with the streamed body and MIME type."""
    backend = S3Backend()
    client = MagicMock()
    client.put_object = AsyncMock(return_value={"ETag": '"deadbeef"'})
    _install_client(backend, client)

    obj = await backend.save(
        "2026/01/01/abc.bin",
        _async_iter([b"hel", b"lo"]),
        content_type="application/octet-stream",
        declared_size=None,
    )

    client.put_object.assert_awaited_once()
    kwargs = client.put_object.call_args.kwargs
    assert kwargs["Bucket"] == _BUCKET
    assert kwargs["Key"] == "2026/01/01/abc.bin"
    assert kwargs["Body"] == b"hello"
    assert kwargs["ContentType"] == "application/octet-stream"
    assert obj.size_bytes == 5
    assert obj.etag == "deadbeef"


async def test_open_yields_chunks_from_get_object(s3_settings):
    """``open`` must stream chunks from the ``Body`` returned by ``get_object``."""
    backend = S3Backend()
    body = MagicMock()

    async def _iter_chunks(_size: int) -> AsyncIterator[bytes]:
        for c in (b"abc", b"def"):
            yield c

    body.iter_chunks = _iter_chunks
    body.close = MagicMock()

    client = MagicMock()
    client.get_object = AsyncMock(return_value={"Body": body})
    _install_client(backend, client)

    received = bytearray()
    async with backend.open("k") as chunks:
        async for chunk in chunks:
            received.extend(chunk)
    assert bytes(received) == b"abcdef"
    body.close.assert_called_once()


async def test_open_translates_nosuchkey_to_filenotfound(s3_settings):
    backend = S3Backend()
    client = MagicMock()
    client.get_object = AsyncMock(side_effect=_client_error("NoSuchKey"))
    _install_client(backend, client)

    with pytest.raises(FileNotFoundError):
        async with backend.open("missing"):
            pass


async def test_stat_returns_metadata(s3_settings):
    backend = S3Backend()
    client = MagicMock()
    client.head_object = AsyncMock(return_value={"ContentLength": 11, "ContentType": "text/plain", "ETag": '"abc123"'})
    _install_client(backend, client)

    info = await backend.stat("k")
    assert info.size_bytes == 11
    assert info.content_type == "text/plain"
    assert info.etag == "abc123"


async def test_stat_translates_404_to_filenotfound(s3_settings):
    backend = S3Backend()
    client = MagicMock()
    client.head_object = AsyncMock(side_effect=_client_error("404"))
    _install_client(backend, client)

    with pytest.raises(FileNotFoundError):
        await backend.stat("missing")


async def test_delete_invokes_delete_object(s3_settings):
    backend = S3Backend()
    client = MagicMock()
    client.delete_object = AsyncMock(return_value={})
    _install_client(backend, client)

    await backend.delete("any/key")
    client.delete_object.assert_awaited_once_with(Bucket=_BUCKET, Key="any/key")


async def test_presigned_url_uses_get_object_with_ttl(s3_settings):
    backend = S3Backend()
    client = MagicMock()
    client.generate_presigned_url = AsyncMock(return_value="https://example/signed")
    _install_client(backend, client)

    url = await backend.presigned_url("k", expires_in=42)
    client.generate_presigned_url.assert_awaited_once()
    args: dict[str, Any] = client.generate_presigned_url.call_args.kwargs
    assert args["Params"] == {"Bucket": _BUCKET, "Key": "k"}
    assert args["ExpiresIn"] == 42
    assert url == "https://example/signed"


def test_construction_without_bucket_raises(monkeypatch):
    monkeypatch.setattr(settings.storage_s3, "bucket", "")
    with pytest.raises(ValueError, match="bucket"):
        S3Backend()
