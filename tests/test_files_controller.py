"""Handler-level tests for the files controller.

The controller is exercised via Litestar's test client against a minimal
app. The SQLAlchemy session, the ORM class, the storage backend and (for
the proxy relay) ``httpx`` are stubbed so the tests need neither a live
PostgreSQL nor a real object store — the focus is on the controller's
flow control (two-phase upload, size enforcement, delete ordering,
optional URL proxying, and the HTTP relay).
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from litestar import Litestar
from litestar.di import Provide
from litestar.testing import TestClient
from litestar.types import ASGIApp, Receive, Scope, Send
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from fusionserve.auth import User
from fusionserve.files.controller import build_controller
from fusionserve.storage.base import StorageObject, UploadTicket

_UPLOAD_URL = "https://s3.example/bucket/2026/01/01/x.bin?X-Amz-Signature=up"
_DOWNLOAD_URL = "https://s3.example/bucket/2026/01/01/x.bin?X-Amz-Signature=down"
_ORIGIN = "https://s3.example"


class _FakeBackend:
    """In-memory backend implementing the presigned Protocol."""

    def __init__(self) -> None:
        self.objects: dict[str, StorageObject] = {}
        self.delete_calls: list[str] = []
        self.upload_url = _UPLOAD_URL
        self.download_url = _DOWNLOAD_URL
        self.origin = _ORIGIN

    async def generate_upload_url(self, key, *, content_type, expires_in) -> UploadTicket:
        return UploadTicket(
            url=self.upload_url,
            method="PUT",
            headers={"Content-Type": content_type},
            expires_at=datetime.datetime.now(datetime.UTC),
        )

    async def generate_download_url(self, key, *, expires_in) -> str:
        return self.download_url

    async def stat(self, key) -> StorageObject:
        if key not in self.objects:
            raise FileNotFoundError(key)
        return self.objects[key]

    async def delete(self, key) -> None:
        self.delete_calls.append(key)
        self.objects.pop(key, None)

    async def object_origin(self) -> str:
        return self.origin


class _FakeRow:
    """Mimic an automap ORM instance well enough for the controller."""

    def __init__(self, **kwargs: Any) -> None:
        self.id = kwargs.pop("id", uuid.uuid4())
        self.filename = kwargs["filename"]
        self.content_type = kwargs["content_type"]
        self.size_bytes = kwargs.get("size_bytes")
        self.storage_key = kwargs["storage_key"]
        self.storage_backend = kwargs["storage_backend"]
        self.status = kwargs.get("status", "pending")
        self.etag = kwargs.get("etag")
        self.attributes = kwargs.get("attributes")
        self.uploaded_by = kwargs.get("uploaded_by")
        self.uploaded_at = kwargs.get("uploaded_at") or datetime.datetime.now(datetime.UTC)


class _FakeSession(AsyncSession):
    """In-memory session implementing the slice of AsyncSession we use."""

    def __init__(self) -> None:
        self.rows: dict[uuid.UUID, _FakeRow] = {}
        self._pending: _FakeRow | None = None

    def add(self, row: _FakeRow) -> None:
        self._pending = row
        self.rows[row.id] = row

    async def flush(self) -> None:
        # Emulate the DB's UNIQUE(storage_key) constraint.
        pending = self._pending
        if pending is not None and any(
            other.id != pending.id and other.storage_key == pending.storage_key for other in self.rows.values()
        ):
            self.rows.pop(pending.id, None)
            raise IntegrityError("INSERT", {}, Exception("duplicate key value violates unique constraint"))

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None

    async def get(self, _cls, pk):
        if isinstance(pk, dict):
            return self.rows.get(pk["id"])
        return self.rows.get(pk)

    async def execute(self, statement):
        return None

    async def delete(self, row):
        self.rows.pop(row.id, None)


# --------------------------------------------------------------------------- #
# Fake httpx used by the proxy relay handlers.
# --------------------------------------------------------------------------- #


class _FakeUpstreamResponse:
    def __init__(self, status_code=200, headers=None, body=b"") -> None:
        self.status_code = status_code
        self.headers = headers or {}
        self._body = body

    async def aiter_bytes(self, _size=65536) -> AsyncIterator[bytes]:
        yield self._body

    async def aread(self) -> bytes:
        return self._body

    async def aclose(self) -> None:
        return None


class _FakeAsyncClient:
    calls: list[dict[str, Any]] = []
    put_response = _FakeUpstreamResponse(status_code=200, headers={"etag": '"abc"'})
    get_response = _FakeUpstreamResponse(status_code=200, headers={"content-type": "text/plain"}, body=b"filedata")

    def __init__(self, timeout=None) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def build_request(self, method, url):
        return {"method": method, "url": url}

    async def request(self, method, url, content=None, headers=None):
        body = bytearray()
        if content is not None:
            async for chunk in content:
                body.extend(chunk)
        _FakeAsyncClient.calls.append({"method": method, "url": url, "body": bytes(body), "headers": headers or {}})
        return _FakeAsyncClient.put_response

    async def send(self, request, stream=False):
        _FakeAsyncClient.calls.append({"method": "GET", "url": request["url"]})
        return _FakeAsyncClient.get_response

    async def aclose(self):
        return None


class _FakeHttpx:
    AsyncClient = _FakeAsyncClient


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


@pytest.fixture(autouse=True)
def _reset_proxy_setting(monkeypatch):
    from fusionserve.config import settings

    monkeypatch.setattr(settings, "storage_proxy_urls", False)


def _build_app(*, backend, session, user, max_single_file: int | None = None, with_user: bool = True):
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
            if scope["type"] == "http":
                scope["user"] = user
                scope["auth"] = None
            await self.app(scope, receive, send)

    from litestar.middleware import DefineMiddleware

    middleware = [DefineMiddleware(_InjectUserMiddleware)] if with_user else []
    return Litestar(
        route_handlers=[controller],
        dependencies={"session": Provide(_session_provider)},
        middleware=middleware,
        debug=True,
    )


def _seed_pending(session, backend, *, key="2026/01/01/x.bin", size=None, uploaded=False):
    row = _FakeRow(
        filename="report.csv",
        content_type="text/csv",
        size_bytes=size,
        storage_key=key,
        storage_backend="fake",
        status="pending",
    )
    session.rows[row.id] = row
    if uploaded:
        backend.objects[key] = StorageObject(key=key, size_bytes=size or 10, content_type="text/csv", etag="e1")
    return row


# --------------------------------------------------------------------------- #
# init
# --------------------------------------------------------------------------- #


def test_init_requires_authentication(fake_backend, fake_session):
    app = _build_app(backend=fake_backend, session=fake_session, user=None)
    with TestClient(app) as client:
        response = client.post("/api/v1/_uploads", json={"files": [{"filename": "a.txt"}]})
    assert response.status_code == 401


def test_init_returns_ticket_and_creates_pending_row(fake_backend, fake_session, authed_user):
    app = _build_app(backend=fake_backend, session=fake_session, user=authed_user)
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/_uploads",
            json={"files": [{"filename": "a.txt", "content_type": "text/plain", "attributes": {"k": "v"}}]},
        )
    assert response.status_code == 201
    body = response.json()
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["filename"] == "a.txt"
    # Storage key is the (sanitized) client filename.
    assert item["storage_key"] == "a.txt"
    assert item["upload_url"] == _UPLOAD_URL
    assert item["method"] == "PUT"
    assert item["headers"] == {"Content-Type": "text/plain"}
    # Pending row persisted with server-side attribution + attributes.
    row = fake_session.rows[uuid.UUID(item["id"])]
    assert row.status == "pending"
    assert row.storage_key == "a.txt"
    assert row.size_bytes is None
    assert row.attributes == {"k": "v"}
    assert row.uploaded_by == authed_user.id


def test_init_multi_file(fake_backend, fake_session, authed_user):
    app = _build_app(backend=fake_backend, session=fake_session, user=authed_user)
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/_uploads",
            json={"files": [{"filename": "a.txt"}, {"filename": "b.bin", "content_type": "application/octet-stream"}]},
        )
    assert response.status_code == 201
    assert [i["filename"] for i in response.json()["items"]] == ["a.txt", "b.bin"]


def test_init_empty_batch_rejected(fake_backend, fake_session, authed_user):
    app = _build_app(backend=fake_backend, session=fake_session, user=authed_user)
    with TestClient(app) as client:
        response = client.post("/api/v1/_uploads", json={"files": []})
    assert response.status_code == 400


def test_init_preserves_directories_and_strips_leading_traversal(fake_backend, fake_session, authed_user):
    """Leading ``../`` is stripped while the directory structure survives."""
    app = _build_app(backend=fake_backend, session=fake_session, user=authed_user)
    with TestClient(app) as client:
        response = client.post("/api/v1/_uploads", json={"files": [{"filename": "../../etc/passwd"}]})
    assert response.status_code == 201
    key = response.json()["items"][0]["storage_key"]
    assert key == "etc/passwd"
    assert ".." not in key


def test_init_collapses_mid_path_traversal(fake_backend, fake_session, authed_user):
    """Runs of 2+ dots anywhere in the path collapse to a single dot."""
    app = _build_app(backend=fake_backend, session=fake_session, user=authed_user)
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/_uploads",
            json={"files": [{"filename": "a/../../b.txt"}, {"filename": "c/..../d.txt"}]},
        )
    assert response.status_code == 201
    keys = [item["storage_key"] for item in response.json()["items"]]
    assert keys == ["a/././b.txt", "c/./d.txt"]
    for key in keys:
        assert ".." not in key
        assert "/" in key


def test_init_duplicate_filename_returns_409(fake_backend, fake_session, authed_user):
    """A second upload of the same filename must be rejected with 409."""
    app = _build_app(backend=fake_backend, session=fake_session, user=authed_user)
    with TestClient(app) as client:
        first = client.post("/api/v1/_uploads", json={"files": [{"filename": "dup.txt"}]})
        assert first.status_code == 201
        second = client.post("/api/v1/_uploads", json={"files": [{"filename": "dup.txt"}]})
    assert second.status_code == 409


def test_init_returns_proxy_url_when_enabled(fake_backend, fake_session, authed_user, monkeypatch):
    from fusionserve.config import settings

    monkeypatch.setattr(settings, "storage_proxy_urls", True)
    app = _build_app(backend=fake_backend, session=fake_session, user=authed_user)
    with TestClient(app) as client:
        response = client.post("/api/v1/_uploads", json={"files": [{"filename": "a.txt"}]})
    url = response.json()["items"][0]["upload_url"]
    assert "/api/v1/_uploads/proxy/bucket/2026/01/01/x.bin" in url
    assert "X-Amz-Signature=up" in url
    assert "s3.example" not in url


# --------------------------------------------------------------------------- #
# complete
# --------------------------------------------------------------------------- #


def test_complete_verifies_and_marks_completed(fake_backend, fake_session, authed_user):
    row = _seed_pending(fake_session, fake_backend, size=123, uploaded=True)
    app = _build_app(backend=fake_backend, session=fake_session, user=authed_user)
    with TestClient(app) as client:
        response = client.post(f"/api/v1/_uploads/{row.id}/complete")
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "completed"
    assert body["size_bytes"] == 123
    assert body["etag"] == "e1"
    assert row.status == "completed"


def test_complete_overwrites_attributes(fake_backend, fake_session, authed_user):
    row = _seed_pending(fake_session, fake_backend, size=10, uploaded=True)
    row.attributes = {"orig": 1}
    app = _build_app(backend=fake_backend, session=fake_session, user=authed_user)
    with TestClient(app) as client:
        response = client.post(f"/api/v1/_uploads/{row.id}/complete", json={"attributes": {"new": 2}})
    assert response.status_code == 201
    assert row.attributes == {"new": 2}


def test_complete_missing_object_returns_409(fake_backend, fake_session, authed_user):
    row = _seed_pending(fake_session, fake_backend, uploaded=False)
    app = _build_app(backend=fake_backend, session=fake_session, user=authed_user)
    with TestClient(app) as client:
        response = client.post(f"/api/v1/_uploads/{row.id}/complete")
    assert response.status_code == 409


def test_complete_oversize_object_rejected_and_cleaned(fake_backend, fake_session, authed_user):
    row = _seed_pending(fake_session, fake_backend, key="k/big.bin", size=999, uploaded=True)
    app = _build_app(backend=fake_backend, session=fake_session, user=authed_user, max_single_file=100)
    with TestClient(app) as client:
        response = client.post(f"/api/v1/_uploads/{row.id}/complete")
    assert response.status_code == 413
    assert "k/big.bin" in fake_backend.delete_calls
    assert row.id not in fake_session.rows


def test_complete_missing_row_returns_404(fake_backend, fake_session, authed_user):
    app = _build_app(backend=fake_backend, session=fake_session, user=authed_user)
    with TestClient(app) as client:
        response = client.post(f"/api/v1/_uploads/{uuid.uuid4()}/complete")
    assert response.status_code == 404


# --------------------------------------------------------------------------- #
# download
# --------------------------------------------------------------------------- #


def test_download_requires_authentication(fake_backend, fake_session):
    row = _seed_pending(fake_session, fake_backend, uploaded=True)
    app = _build_app(backend=fake_backend, session=fake_session, user=None)
    with TestClient(app) as client:
        response = client.get(f"/api/v1/_uploads/{row.id}/content", follow_redirects=False)
    assert response.status_code == 401


def test_download_redirects_to_presigned_url(fake_backend, fake_session, authed_user):
    row = _seed_pending(fake_session, fake_backend, uploaded=True)
    app = _build_app(backend=fake_backend, session=fake_session, user=authed_user)
    with TestClient(app) as client:
        response = client.get(f"/api/v1/_uploads/{row.id}/content", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == _DOWNLOAD_URL


def test_download_redirects_to_proxy_url_when_enabled(fake_backend, fake_session, authed_user, monkeypatch):
    from fusionserve.config import settings

    monkeypatch.setattr(settings, "storage_proxy_urls", True)
    row = _seed_pending(fake_session, fake_backend, uploaded=True)
    app = _build_app(backend=fake_backend, session=fake_session, user=authed_user)
    with TestClient(app) as client:
        response = client.get(f"/api/v1/_uploads/{row.id}/content", follow_redirects=False)
    assert response.status_code == 302
    location = response.headers["location"]
    assert "/api/v1/_uploads/proxy/bucket/2026/01/01/x.bin" in location
    assert "X-Amz-Signature=down" in location


def test_download_missing_row_returns_404(fake_backend, fake_session, authed_user):
    app = _build_app(backend=fake_backend, session=fake_session, user=authed_user)
    with TestClient(app) as client:
        response = client.get(f"/api/v1/_uploads/{uuid.uuid4()}/content", follow_redirects=False)
    assert response.status_code == 404


# --------------------------------------------------------------------------- #
# delete
# --------------------------------------------------------------------------- #


def test_delete_calls_backend_then_removes_row(fake_backend, fake_session, authed_user):
    row = _seed_pending(fake_session, fake_backend, key="seed/key.txt", size=3, uploaded=True)
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


# --------------------------------------------------------------------------- #
# proxy relay
# --------------------------------------------------------------------------- #


def test_proxy_upload_relays_body_to_object_store(fake_backend, fake_session, monkeypatch):
    from fusionserve.files import controller as controller_module

    _FakeAsyncClient.calls = []
    monkeypatch.setattr(controller_module, "httpx", _FakeHttpx)
    app = _build_app(backend=fake_backend, session=fake_session, user=None, with_user=False)
    with TestClient(app) as client:
        response = client.put(
            "/api/v1/_uploads/proxy/bucket/2026/01/01/x.bin?X-Amz-Signature=up",
            content=b"hello world",
            headers={"Content-Type": "image/png"},
        )
    assert response.status_code == 200
    assert response.headers.get("etag") == '"abc"'
    call = _FakeAsyncClient.calls[-1]
    assert call["method"] == "PUT"
    assert call["url"] == f"{_ORIGIN}/bucket/2026/01/01/x.bin?X-Amz-Signature=up"
    assert call["body"] == b"hello world"
    assert call["headers"].get("Content-Type") == "image/png"


def test_proxy_download_streams_body_from_object_store(fake_backend, fake_session, monkeypatch):
    from fusionserve.files import controller as controller_module

    _FakeAsyncClient.calls = []
    monkeypatch.setattr(controller_module, "httpx", _FakeHttpx)
    app = _build_app(backend=fake_backend, session=fake_session, user=None, with_user=False)
    with TestClient(app) as client:
        response = client.get("/api/v1/_uploads/proxy/bucket/2026/01/01/x.bin?X-Amz-Signature=down")
    assert response.status_code == 200
    assert response.content == b"filedata"
    assert response.headers["content-type"].startswith("text/plain")
    call = _FakeAsyncClient.calls[-1]
    assert call["url"] == f"{_ORIGIN}/bucket/2026/01/01/x.bin?X-Amz-Signature=down"
