"""Unit tests for :mod:`fusionserve.storage` loader/dispatcher.

These tests touch only the loader helpers — no network, no S3. The
bundled backends are imported to ensure ``load_backend`` short-circuits
on the ``s3`` / ``azure`` literals, and the dotted-path branch is
exercised against an in-memory dummy class.
"""

from __future__ import annotations

import sys
import types

import pytest

from fusionserve.storage import StorageBackend, load_backend


class _DummyBackend:
    """Minimal stub satisfying the :class:`StorageBackend` protocol."""

    async def generate_upload_url(self, key, *, content_type, expires_in):
        raise NotImplementedError

    async def generate_download_url(self, key, *, expires_in):
        raise NotImplementedError

    async def delete(self, key):
        return None

    async def stat(self, key):
        raise NotImplementedError

    async def object_origin(self):
        raise NotImplementedError


def _register_dummy_module(name: str, **attrs: object) -> None:
    """Inject a synthetic module into ``sys.modules`` for the test."""
    module = types.ModuleType(name)
    for attr_name, value in attrs.items():
        setattr(module, attr_name, value)
    sys.modules[name] = module


def test_load_backend_azure_literal_returns_placeholder():
    """The ``azure`` literal must resolve to the placeholder backend."""
    backend = load_backend("azure")
    assert type(backend).__name__ == "AzureBlobBackend"
    assert isinstance(backend, StorageBackend)


async def test_azure_placeholder_methods_raise_not_implemented():
    """Every placeholder method must fail loudly rather than no-op."""
    backend = load_backend("azure")
    with pytest.raises(NotImplementedError):
        await backend.generate_upload_url("k", content_type="text/plain", expires_in=60)
    with pytest.raises(NotImplementedError):
        await backend.generate_download_url("k", expires_in=60)
    with pytest.raises(NotImplementedError):
        await backend.stat("k")
    with pytest.raises(NotImplementedError):
        await backend.object_origin()


def test_load_backend_s3_literal_returns_s3_backend(monkeypatch):
    """The ``s3`` literal must resolve to the bundled S3 backend.

    The backend instantiation requires a bucket name; we set one
    temporarily and rely on the lazy session — no AWS call is made
    during construction.
    """
    from fusionserve.config import settings

    monkeypatch.setattr(settings.storage_s3, "bucket", "test-bucket")
    backend = load_backend("s3")
    assert type(backend).__name__ == "S3Backend"


def test_load_backend_dotted_path_imports_custom_class():
    """A ``pkg.mod:Class`` spec must be importable and instantiated."""
    _register_dummy_module("fusionserve_test_dummy_backend", DummyBackend=_DummyBackend)
    try:
        backend = load_backend("fusionserve_test_dummy_backend:DummyBackend")
        assert isinstance(backend, _DummyBackend)
    finally:
        sys.modules.pop("fusionserve_test_dummy_backend", None)


def test_load_backend_rejects_unknown_literal():
    """Unknown short literals (missing the ``:``) must raise ``ValueError``."""
    with pytest.raises(ValueError, match="Unknown storage backend"):
        load_backend("nope")


def test_load_backend_missing_class_raises_importerror():
    """A dotted path naming a missing attribute must raise ``ImportError``."""
    _register_dummy_module("fusionserve_test_empty_backend")
    try:
        with pytest.raises(ImportError):
            load_backend("fusionserve_test_empty_backend:Missing")
    finally:
        sys.modules.pop("fusionserve_test_empty_backend", None)


def test_load_backend_rejects_non_class_target():
    """Resolving a non-class attribute must raise ``TypeError``."""

    def not_a_class():
        return None

    _register_dummy_module("fusionserve_test_callable_backend", target=not_a_class)
    try:
        with pytest.raises(TypeError, match="not a class"):
            load_backend("fusionserve_test_callable_backend:target")
    finally:
        sys.modules.pop("fusionserve_test_callable_backend", None)


def test_load_backend_rejects_non_protocol_class():
    """A class that doesn't satisfy the protocol must be rejected."""

    class _Bad:
        # Missing every required method.
        pass

    _register_dummy_module("fusionserve_test_bad_backend", Bad=_Bad)
    try:
        with pytest.raises(TypeError, match="does not implement StorageBackend"):
            load_backend("fusionserve_test_bad_backend:Bad")
    finally:
        sys.modules.pop("fusionserve_test_bad_backend", None)
