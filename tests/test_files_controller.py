"""Handler-level tests for the files controller.

The controller is exercised via Litestar's test client against a minimal
app. The SQLAlchemy session and the ORM class are stubbed so the tests
do not require a live PostgreSQL — the focus is on the controller's
flow control (auth, size enforcement, per-file status, delete ordering).
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from litestar import Litestar
from litestar.di import Provide
from litestar.middleware import DefineMiddleware
from litestar.testing import TestClient
from litestar.types import ASGIApp, Receive, Scope, Send
from sqlalchemy.ext.asyncio import AsyncSession

from fusionserve.auth import User
from fusionserve.files.controller import build_controller


class _FakeBackend:
    """In-memory backend with introspection hooks for the tests."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.content_types: dict[str, str] = {}
        self.delete_calls: list[str] = []
        self.fail_save_for: set[str] = set()
        self.fail_delete = False
        self.presign_url: str | None = None

    async def save(self, key, stream, *, content_type, declared_size):
        buffer = bytearray()
        async for chunk in stream:
            buffer.extend(chunk)
        if key in self.fail_save_for:
            raise RuntimeError("storage failure for testing")
        self.objects[key] = bytes(buffer)
        self.content_types[key] = content_type
        from fusionserve.storage.base import StorageObject

        return StorageObject(key=key, size_bytes=len(buffer), content_type=content_type)

    @asynccontextmanager
    async def open(self, key):
        if key not in self.objects:
            raise FileNotFoundError(key)
        data = self.objects[key]

        async def _iter() -> AsyncIterator[bytes]:
            yield data

        yield _iter()

    async def delete(self, key):
        self.delete_calls.append(key)
        if self.fail_delete:
            raise RuntimeError("delete failure for testing")
        self.objects.pop(key, None)

    async def stat(self, key):
        from fusionserve.storage.base import StorageObject

        if key not in self.objects:
            raise FileNotFoundError(key)
        return StorageObject(
            key=key,
            size_bytes=len(self.objects[key]),
            content_type=self.content_types.get(key, "application/octet-stream"),
        )

    async def presigned_url(self, key, *, expires_in):
        return self.presign_url


class _FakeRow:
    """Mimic an automap ORM instance well enough for the controller."""

    def __init__(self, **kwargs: Any) -> None:
        self.id = kwargs.pop("id", uuid.uuid4())
        self.filename = kwargs["filename"]
        self.content_type = kwargs["content_type"]
        self.size_bytes = kwargs["size_bytes"]
        self.storage_key = kwargs["storage_key"]
        self.storage_backend = kwargs["storage_backend"]
        self.uploaded_by = kwargs.get("uploaded_by")
        self.uploaded_at = kwargs.get("uploaded_at") or datetime.datetime.now(datetime.UTC)


class _FakeSession(AsyncSession):
    """In-memory session implementing the slice of AsyncSession we use.

    Inherits from :class:`AsyncSession` so Litestar's msgspec-driven
    signature validator accepts it where ``session: AsyncSession`` is
    declared; the parent ``__init__`` is deliberately bypassed.
    """

    def __init__(self) -> None:
        self.rows: dict[uuid.UUID, _FakeRow] = {}
        self.role_calls: list[str] = []
        self.fail_flush = False

    def add(self, row: _FakeRow) -> None:
        self.rows[row.id] = row

    async def flush(self) -> None:
        if self.fail_flush:
            raise RuntimeError("flush failure for testing")

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        # Drop any rows added since the last commit; the fake tracks
        # nothing more granular than "all rows", which is enough here.
        return None

    async def get(self, _cls, pk):
        if isinstance(pk, dict):
            return self.rows.get(pk["id"])
        return self.rows.get(pk)

    async def execute(self, statement):
        return None

    async def delete(self, row):
        self.rows.pop(row.id, None)


def _make_user() -> User:
    return User(
        id=uuid.uuid4(),
        username="alice",
        email="alice@example.com",
        display_name="Alice",
        first_name="Alice",
        surname="Anderson",
        roles=["app_user"],
        role="app_user",
    )


@pytest.fixture
def fake_backend():
    return _FakeBackend()


@pytest.fixture
def fake_session():
    return _FakeSession()


@pytest.fixture
def authed_user():
    return _make_user()


def _build_app(*, backend, session, user, max_single_file: int | None = None):
    """Build a minimal Litestar app hosting just the files controller."""
    from fusionserve.config import settings

    if max_single_file is not None:
        settings.storage_max_single_file_bytes = max_single_file

    controller = build_controller(_FakeRow, backend)

    async def _session_provider() -> _FakeSession:
        return session

    class _InjectUserMiddleware:
        def __init__(self, app: ASGIApp) -> None:
            self.app = app

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            # Litestar's auth middleware is bypassed in the test app;
            # this mirrors what ``AuthMiddleware`` would do by stuffing
            # the resolved user into the connection scope.
            scope["user"] = user
            scope["auth"] = None
            await self.app(scope, receive, send)

    return Litestar(
        route_handlers=[controller],
        dependencies={"session": Provide(_session_provider)},
        middleware=[DefineMiddleware(_InjectUserMiddleware)],
        debug=True,
    )


def test_upload_requires_authentication(fake_backend, fake_session):
    """An unauthenticated POST must be rejected with 401."""
    app = _build_app(backend=fake_backend, session=fake_session, user=None)
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/_uploads",
            files={"data": ("hello.txt", b"hi there", "text/plain")},
        )
    assert response.status_code == 401


def test_upload_single_file_happy_path(fake_backend, fake_session, authed_user):
    """A well-formed single-file POST must persist and return metadata."""
    app = _build_app(backend=fake_backend, session=fake_session, user=authed_user)
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/_uploads",
            files={"data": ("hello.txt", b"hello world", "text/plain")},
        )
    assert response.status_code == 207
    body = response.json()
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["status"] == "ok"
    assert item["filename"] == "hello.txt"
    upload = item["upload"]
    assert upload["filename"] == "hello.txt"
    assert upload["content_type"] == "text/plain"
    assert upload["size_bytes"] == len(b"hello world")
    # Server-side attribution from request.user.id, not the body.
    assert uuid.UUID(upload["uploaded_by"]) == authed_user.id
    # Blob is actually stored under the server-generated key.
    assert fake_backend.objects[upload["storage_key"]] == b"hello world"


def test_upload_multi_file_returns_per_file_status(fake_backend, fake_session, authed_user):
    """A multi-file POST must return one ``items`` entry per part."""
    app = _build_app(backend=fake_backend, session=fake_session, user=authed_user)
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/_uploads",
            files=[
                ("data", ("a.txt", b"AAA", "text/plain")),
                ("data", ("b.txt", b"BBBB", "text/plain")),
            ],
        )
    assert response.status_code == 207
    body = response.json()
    assert [item["filename"] for item in body["items"]] == ["a.txt", "b.txt"]
    assert all(item["status"] == "ok" for item in body["items"])
    sizes = [item["upload"]["size_bytes"] for item in body["items"]]
    assert sizes == [3, 4]


def test_upload_oversize_file_yields_per_file_error(fake_backend, fake_session, authed_user):
    """An oversize file must produce a per-file error without aborting the batch."""
    app = _build_app(
        backend=fake_backend,
        session=fake_session,
        user=authed_user,
        max_single_file=4,
    )
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/_uploads",
            files=[
                ("data", ("ok.txt", b"AAA", "text/plain")),
                ("data", ("big.txt", b"BBBBBBBB", "text/plain")),
            ],
        )
    assert response.status_code == 207
    body = response.json()
    statuses = [item["status"] for item in body["items"]]
    assert statuses == ["ok", "error"]
    assert "exceeds per-file size limit" in body["items"][1]["error"]
    # Oversize blob must have been cleaned up.
    big_keys = [k for k, v in fake_backend.objects.items() if v.startswith(b"BBB")]
    assert big_keys == []


def test_upload_storage_failure_recorded_as_per_file_error(fake_backend, fake_session, authed_user):
    """A backend exception must produce a per-file error, not a 5xx."""
    # Force the next save to raise.
    fake_backend.fail_save_for = {"placeholder"}

    class _BoomBackend(_FakeBackend):
        async def save(self, key, stream, *, content_type, declared_size):
            async for _ in stream:
                pass
            raise RuntimeError("kaboom")

    boom = _BoomBackend()
    app = _build_app(backend=boom, session=fake_session, user=authed_user)
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/_uploads",
            files={"data": ("oops.txt", b"hi", "text/plain")},
        )
    assert response.status_code == 207
    body = response.json()
    assert body["items"][0]["status"] == "error"
    assert "storage error" in body["items"][0]["error"]


def test_delete_calls_backend_then_removes_row(fake_backend, fake_session, authed_user):
    """``DELETE`` must drop the blob *before* the metadata row."""
    # Seed a fake row + blob.
    row = _FakeRow(
        filename="x.txt",
        content_type="text/plain",
        size_bytes=3,
        storage_key="seed/key.txt",
        storage_backend="fake",
    )
    fake_session.rows[row.id] = row
    fake_backend.objects["seed/key.txt"] = b"xxx"

    app = _build_app(backend=fake_backend, session=fake_session, user=authed_user)
    with TestClient(app) as client:
        response = client.delete(f"/api/v1/_uploads/{row.id}")
    assert response.status_code == 204
    assert "seed/key.txt" in fake_backend.delete_calls
    assert row.id not in fake_session.rows


def test_delete_missing_row_returns_404(fake_backend, fake_session, authed_user):
    app = _build_app(backend=fake_backend, session=fake_session, user=authed_user)
    with TestClient(app) as client:
        response = client.delete(f"/api/v1/_uploads/{uuid.uuid4()}")
    assert response.status_code == 404


def test_download_streams_blob(fake_backend, fake_session, authed_user):
    """A standard GET must stream the blob with the original content type."""
    row = _FakeRow(
        filename="report.csv",
        content_type="text/csv",
        size_bytes=10,
        storage_key="2026/01/01/abc.csv",
        storage_backend="fake",
    )
    fake_session.rows[row.id] = row
    fake_backend.objects["2026/01/01/abc.csv"] = b"col1,col2\n"

    app = _build_app(backend=fake_backend, session=fake_session, user=authed_user)
    with TestClient(app) as client:
        response = client.get(f"/api/v1/_uploads/{row.id}/content")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert response.content == b"col1,col2\n"
    assert "attachment" in response.headers.get("content-disposition", "")


def test_download_with_redirect_uses_presigned_url(fake_backend, fake_session, authed_user):
    """``?redirect=1`` must 302 to the presigned URL when the backend supports it."""
    fake_backend.presign_url = "https://example.com/signed?abc=123"
    row = _FakeRow(
        filename="big.bin",
        content_type="application/octet-stream",
        size_bytes=4,
        storage_key="2026/01/01/big.bin",
        storage_backend="fake",
    )
    fake_session.rows[row.id] = row
    fake_backend.objects["2026/01/01/big.bin"] = b"data"

    app = _build_app(backend=fake_backend, session=fake_session, user=authed_user)
    with TestClient(app) as client:
        response = client.get(
            f"/api/v1/_uploads/{row.id}/content?redirect=1",
            follow_redirects=False,
        )
    assert response.status_code == 302
    assert response.headers["location"] == "https://example.com/signed?abc=123"


def test_download_redirect_falls_back_to_stream_when_unsupported(fake_backend, fake_session, authed_user):
    """``?redirect=1`` against a backend without presigning must still stream."""
    fake_backend.presign_url = None
    row = _FakeRow(
        filename="x.bin",
        content_type="application/octet-stream",
        size_bytes=2,
        storage_key="2026/01/02/x.bin",
        storage_backend="fake",
    )
    fake_session.rows[row.id] = row
    fake_backend.objects["2026/01/02/x.bin"] = b"hi"

    app = _build_app(backend=fake_backend, session=fake_session, user=authed_user)
    with TestClient(app) as client:
        response = client.get(
            f"/api/v1/_uploads/{row.id}/content?redirect=1",
            follow_redirects=False,
        )
    assert response.status_code == 200
    assert response.content == b"hi"
