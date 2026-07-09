"""Storage backend loader and registry.

Exposes :func:`load_backend` (used at startup) and :func:`get_storage`
(an lru-cached singleton accessor used by request handlers). Backends are
selected by the ``storage_backend`` setting:

* ``"s3"`` → :class:`fusionserve.storage.s3.S3Backend`
* ``"azure"`` → :class:`fusionserve.storage.azure.AzureBlobBackend`
  (placeholder — raises ``NotImplementedError`` on use)
* anything else is treated as a dotted import path
  ``"pkg.mod:ClassName"`` and resolved via :mod:`importlib`.
"""

from __future__ import annotations

import importlib
import logging
from functools import lru_cache

from ..config import settings
from .base import StorageBackend, StorageObject, UploadTicket

_logger = logging.getLogger(settings.app_name)

__all__ = ["StorageBackend", "StorageObject", "UploadTicket", "get_storage", "load_backend"]


def load_backend(spec: str) -> StorageBackend:
    """Instantiate the storage backend identified by ``spec``.

    The short literals ``"s3"`` and ``"azure"`` resolve to the bundled
    backends. Any other value is treated as a ``"pkg.mod:Class"`` dotted
    import path; the class is imported and instantiated with no
    constructor arguments (backends read their own settings from
    :mod:`fusionserve.config`).

    Args:
        spec: The backend identifier from ``settings.storage_backend``.

    Returns:
        An instance of a class that satisfies :class:`StorageBackend`.

    Raises:
        ImportError: If the dotted path cannot be resolved.
        TypeError: If the resolved object is not a class implementing the
            :class:`StorageBackend` protocol.
        ValueError: If ``spec`` is not a recognised literal and does not
            contain the ``module:Class`` separator.
    """
    if spec == "s3":
        from .s3 import S3Backend

        return S3Backend()
    if spec == "azure":
        from .azure import AzureBlobBackend

        return AzureBlobBackend()

    if ":" not in spec:
        raise ValueError(
            f"Unknown storage backend {spec!r}. Use 's3', 'azure', or a 'pkg.mod:ClassName' dotted import path."
        )
    module_path, _, class_name = spec.partition(":")
    module = importlib.import_module(module_path)
    try:
        cls = getattr(module, class_name)
    except AttributeError as exc:
        raise ImportError(f"Module {module_path!r} has no attribute {class_name!r}") from exc
    if not isinstance(cls, type):
        raise TypeError(f"{spec!r} resolved to {cls!r}, which is not a class")
    instance = cls()
    if not isinstance(instance, StorageBackend):
        raise TypeError(f"{spec!r} does not implement StorageBackend protocol")
    return instance


@lru_cache(maxsize=1)
def get_storage() -> StorageBackend:
    """Return the configured storage backend, instantiated lazily.

    Cached so every caller receives the same instance for the lifetime of
    the process. Tests that need to swap the backend call
    ``get_storage.cache_clear()`` after mutating :mod:`fusionserve.config`.
    """
    backend = load_backend(settings.storage_backend)
    _logger.info("Storage backend ready: %s", type(backend).__name__)
    return backend
