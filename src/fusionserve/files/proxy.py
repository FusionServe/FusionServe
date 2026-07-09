"""URL rewriting for the optional upload/download HTTP proxy.

When ``settings.storage_proxy_urls`` is on, the presigned URLs handed to
clients are **origin-swapped**: the object store's ``scheme://host`` is
replaced with FusionServe's own base plus the proxy path prefix, while
the path and query (which carry the ``X-Amz-*`` signature) are preserved
verbatim. The relay handlers in :mod:`fusionserve.files.controller`
reverse this by prepending the backend's canonical origin (from
:meth:`fusionserve.storage.base.StorageBackend.object_origin`) to the
captured path — so a relayed request validates against the object store
unchanged.

Because the target origin is derived solely from the backend (never from
client input), the relay cannot be pointed at an arbitrary host.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from ..config import settings


def rewrite_to_proxy(presigned_url: str, *, request_base_url: str) -> str:
    """Rewrite ``presigned_url`` to route through the FusionServe proxy.

    Args:
        presigned_url: The object-store presigned URL.
        request_base_url: The ``scheme://host`` (optionally with a
            trailing slash) of the incoming request, used as the proxy's
            public origin so it works behind reverse proxies.

    Returns:
        A URL with the same path and query as ``presigned_url`` but whose
        origin points at the ``_proxy`` relay under
        ``<request_base_url><base_path>/v1/_uploads``.
    """
    parts = urlsplit(presigned_url)
    base = request_base_url.rstrip("/")
    proxied = f"{base}{settings.base_path}/v1/_uploads/_proxy{parts.path}"
    if parts.query:
        proxied = f"{proxied}?{parts.query}"
    return proxied


def build_target_url(origin: str, s3path: str, query: str) -> str:
    """Reconstruct the object-store URL from the relay's captured parts.

    Args:
        origin: The backend's canonical ``scheme://host``.
        s3path: The path captured by the ``{s3path:path}`` route param
            (no leading slash).
        query: The raw query string from the incoming request (may be
            empty).

    Returns:
        The fully-qualified object-store URL to relay to.
    """
    target = f"{origin.rstrip('/')}/{s3path.lstrip('/')}"
    if query:
        target = f"{target}?{query}"
    return target


__all__ = ["build_target_url", "rewrite_to_proxy"]
