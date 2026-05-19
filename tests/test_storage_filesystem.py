"""Tests for :class:`fusionserve.storage.filesystem.FilesystemBackend`.

The backend has no external dependencies, so a ``tmp_path`` fixture is
all we need to exercise the full lifecycle.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from fusionserve.storage.filesystem import FilesystemBackend


async def _async_iter(chunks: list[bytes]) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk


@pytest.fixture
def backend(tmp_path):
    return FilesystemBackend(root=tmp_path)


async def test_save_writes_chunks_and_returns_size(backend, tmp_path):
    """``save`` must persist the streamed bytes and report the exact size."""
    chunks = [b"hello ", b"world"]
    obj = await backend.save(
        "a/b/c.txt",
        _async_iter(chunks),
        content_type="text/plain",
        declared_size=None,
    )
    assert obj.size_bytes == len(b"hello world")
    assert obj.content_type == "text/plain"
    written = (tmp_path / "a/b/c.txt").read_bytes()
    assert written == b"hello world"


async def test_save_does_not_leak_partial_files_on_failure(backend, tmp_path):
    """A stream that raises mid-write must not leave a final file behind."""

    async def _boom() -> AsyncIterator[bytes]:
        yield b"abc"
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await backend.save("x/y.bin", _boom(), content_type="application/octet-stream", declared_size=None)
    # Final file never created.
    assert not (tmp_path / "x/y.bin").exists()
    # No partial files lingering.
    partial = list((tmp_path / "x").glob("*.partial"))
    assert partial == []


async def test_open_streams_back_what_was_written(backend):
    """``open`` must yield the same bytes that ``save`` consumed."""
    await backend.save(
        "round-trip.dat",
        _async_iter([b"alpha", b"beta", b"gamma"]),
        content_type="application/octet-stream",
        declared_size=None,
    )
    received = bytearray()
    async with backend.open("round-trip.dat") as chunks:
        async for chunk in chunks:
            received.extend(chunk)
    assert bytes(received) == b"alphabetagamma"


async def test_open_missing_key_raises_filenotfound(backend):
    with pytest.raises(FileNotFoundError):
        async with backend.open("does/not/exist.bin"):
            pass


async def test_stat_returns_metadata(backend):
    await backend.save(
        "thing.txt",
        _async_iter([b"12345"]),
        content_type="text/plain",
        declared_size=None,
    )
    info = await backend.stat("thing.txt")
    assert info.size_bytes == 5
    assert info.content_type == "text/plain"


async def test_stat_missing_key_raises_filenotfound(backend):
    with pytest.raises(FileNotFoundError):
        await backend.stat("nope.bin")


async def test_delete_is_idempotent(backend):
    """``delete`` on a non-existent key must succeed silently."""
    await backend.save(
        "to-remove.bin",
        _async_iter([b"x"]),
        content_type="application/octet-stream",
        declared_size=None,
    )
    await backend.delete("to-remove.bin")
    await backend.delete("to-remove.bin")  # idempotent, no exception
    with pytest.raises(FileNotFoundError):
        await backend.stat("to-remove.bin")


async def test_presigned_url_returns_none(backend):
    """Filesystem backend does not support presigned URLs."""
    assert await backend.presigned_url("anything", expires_in=60) is None


async def test_path_traversal_is_rejected(backend):
    """Keys that escape the storage root must be rejected."""
    with pytest.raises(ValueError, match="escapes storage root"):
        await backend.save(
            "../outside.bin",
            _async_iter([b"nope"]),
            content_type="application/octet-stream",
            declared_size=None,
        )
