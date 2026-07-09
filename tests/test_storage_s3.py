"""Contract-level tests for :class:`fusionserve.storage.s3.S3Backend`.

These tests mock the aioboto3 client layer instead of standing up a
moto-backed S3. Rationale:

* aiobotocore (≥ 2.25) and moto don't share an HTTP-stubbing contract,
  so the round-trip path is brittle across patch releases.
* The S3 backend itself is a thin adapter — the value of testing it is
  pinning the wire-level operations it invokes
  (``generate_presigned_url``, ``head_object``, ``delete_object``), not
  re-testing S3 semantics.
* The "mock the boundary" pattern matches the rest of the FusionServe
  unit suite (see AGENTS.md → Tests).

Integration coverage against a live MinIO or real S3 is out of scope of
the unit suite.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest
from botocore.exceptions import ClientError

from fusionserve.config import settings
from fusionserve.storage.s3 import S3Backend

_BUCKET = "fusionserve-test"


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


async def test_generate_upload_url_signs_put_with_content_type(s3_settings):
    """``generate_upload_url`` must presign put_object and bind ContentType."""
    backend = S3Backend()
    client = MagicMock()
    client.generate_presigned_url = AsyncMock(return_value="https://example/put?sig=1")
    _install_client(backend, client)

    ticket = await backend.generate_upload_url("2026/01/01/x.bin", content_type="image/png", expires_in=99)

    client.generate_presigned_url.assert_awaited_once()
    kwargs = client.generate_presigned_url.call_args.kwargs
    assert client.generate_presigned_url.call_args.args[0] == "put_object"
    assert kwargs["Params"] == {"Bucket": _BUCKET, "Key": "2026/01/01/x.bin", "ContentType": "image/png"}
    assert kwargs["ExpiresIn"] == 99
    assert ticket.url == "https://example/put?sig=1"
    assert ticket.method == "PUT"
    assert ticket.headers == {"Content-Type": "image/png"}


async def test_generate_download_url_signs_get(s3_settings):
    backend = S3Backend()
    client = MagicMock()
    client.generate_presigned_url = AsyncMock(return_value="https://example/get?sig=2")
    _install_client(backend, client)

    url = await backend.generate_download_url("k", expires_in=42)
    kwargs = client.generate_presigned_url.call_args.kwargs
    assert client.generate_presigned_url.call_args.args[0] == "get_object"
    assert kwargs["Params"] == {"Bucket": _BUCKET, "Key": "k"}
    assert kwargs["ExpiresIn"] == 42
    assert url == "https://example/get?sig=2"


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


async def test_object_origin_parses_scheme_and_host(s3_settings):
    """``object_origin`` must return scheme://host of a presigned URL."""
    backend = S3Backend()
    client = MagicMock()
    client.generate_presigned_url = AsyncMock(
        return_value="https://fusionserve-test.s3.amazonaws.com/probe?X-Amz-Signature=zzz"
    )
    _install_client(backend, client)

    origin = await backend.object_origin()
    assert origin == "https://fusionserve-test.s3.amazonaws.com"
    # Cached: a second call must not re-presign.
    await backend.object_origin()
    client.generate_presigned_url.assert_awaited_once()


def test_construction_without_bucket_raises(monkeypatch):
    monkeypatch.setattr(settings.storage_s3, "bucket", "")
    with pytest.raises(ValueError, match="bucket"):
        S3Backend()


async def test_object_origin_honours_endpoint_url(monkeypatch):
    """A custom endpoint (MinIO) must surface in the derived origin."""
    monkeypatch.setattr(settings.storage_s3, "bucket", _BUCKET)
    backend = S3Backend()
    client = MagicMock()
    client.generate_presigned_url = AsyncMock(return_value="http://minio:9000/fusionserve-test/probe?sig=1")
    _install_client(backend, client)

    origin = await backend.object_origin()
    assert origin == "http://minio:9000"
