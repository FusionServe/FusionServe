"""Unit tests for ``fusionserve.ui``.

The module surface is intentionally tiny — a :class:`RedirectRenderPlugin`
that diverts ``<base_path>/`` traffic to the React SPA, plus a
:func:`build_spa_route_handler` factory that returns the static-files
router serving the SPA bundle. The tests below pin the router-shape
invariants a future refactor must not silently break.
"""

from __future__ import annotations

from fusionserve.config import settings
from fusionserve.ui import build_spa_route_handler


def _normalize(path: str) -> str:
    """Strip trailing slashes the way Litestar's ``Router`` does."""
    return path.rstrip("/") or "/"


def test_build_spa_route_handler_mounts_at_ui_path():
    """The static-files router must be mounted at ``settings.ui_path``.

    Vite is configured to emit relative asset URLs (``./assets/...``)
    so the chunks are served by the same router from
    ``<ui_path>/assets/...`` — no separate asset router exists.
    """
    router = build_spa_route_handler()
    assert _normalize(router.path) == _normalize(settings.ui_path)


def test_spa_router_is_excluded_from_auth():
    """The static router must opt out of auth via the ``exclude_from_auth`` key.

    The auth middleware (configured in :mod:`fusionserve.main`) honours
    Litestar's ``exclude_opt_key`` mechanism, so any handler / router
    carrying ``opt={"exclude_from_auth": True}`` is skipped without
    needing the URL pattern added to the middleware's ``exclude``
    list. Losing the opt would silently re-enable auth on every static
    asset.
    """
    router = build_spa_route_handler()
    assert router.opt.get("exclude_from_auth") is True, f"router at {router.path!r} missing exclude_from_auth opt"
