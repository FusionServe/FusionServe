"""Unit tests for :mod:`fusionserve.files.proxy` URL rewriting."""

from __future__ import annotations

from fusionserve.config import settings
from fusionserve.files.proxy import build_target_url, rewrite_to_proxy

_PREFIX = f"{settings.base_path}/v1/_uploads/_proxy"


def test_rewrite_swaps_origin_and_preserves_path_and_query():
    url = "https://bucket.s3.eu-central-1.amazonaws.com/2026/05/19/abc.bin?X-Amz-Signature=sig&X-Amz-Expires=3600"
    out = rewrite_to_proxy(url, request_base_url="https://fusionserve.example/")
    assert out == f"https://fusionserve.example{_PREFIX}/2026/05/19/abc.bin?X-Amz-Signature=sig&X-Amz-Expires=3600"


def test_rewrite_without_query():
    url = "https://bucket.s3.amazonaws.com/key"
    out = rewrite_to_proxy(url, request_base_url="https://fs.example")
    assert out == f"https://fs.example{_PREFIX}/key"


def test_rewrite_handles_path_style_bucket():
    """Path-style URLs keep the bucket segment inside the preserved path."""
    url = "http://minio:9000/bucket/2026/05/19/abc.bin?sig=1"
    out = rewrite_to_proxy(url, request_base_url="https://fs.example")
    assert out == f"https://fs.example{_PREFIX}/bucket/2026/05/19/abc.bin?sig=1"


def test_build_target_reconstructs_object_url():
    target = build_target_url(
        "https://bucket.s3.amazonaws.com",
        "2026/05/19/abc.bin",
        "X-Amz-Signature=sig",
    )
    assert target == "https://bucket.s3.amazonaws.com/2026/05/19/abc.bin?X-Amz-Signature=sig"


def test_build_target_without_query():
    target = build_target_url("http://minio:9000", "bucket/key", "")
    assert target == "http://minio:9000/bucket/key"


def test_rewrite_then_build_target_roundtrips_signature():
    """The path+query surviving the round trip is what preserves the signature."""
    origin = "https://bucket.s3.amazonaws.com"
    original = f"{origin}/2026/05/19/abc.bin?X-Amz-Signature=sig&x=1"
    proxied = rewrite_to_proxy(original, request_base_url="https://fs.example")
    # Simulate the relay: strip the proxy prefix to recover the captured path.
    prefix = f"https://fs.example{_PREFIX}/"
    remainder = proxied[len(prefix) :]
    s3path, _, query = remainder.partition("?")
    assert build_target_url(origin, s3path, query) == original
