"""Unit tests for ``fusionserve.ui``.

The module surface is intentionally tiny — a :class:`RedirectRenderPlugin`
that diverts ``<base_path>/`` traffic to the React SPA, plus a
:func:`build_spa_route_handler` factory that returns the SPA route handlers
(an assets static-files router and a base-href-injecting ``index.html``
handler). The tests below pin the shape invariants a future refactor must
not silently break.
"""

from __future__ import annotations

from litestar.router import Router

from fusionserve.config import settings
from fusionserve.ui import _render_index, build_spa_route_handler


def _normalize(path: str) -> str:
    """Strip trailing slashes the way Litestar's ``Router`` does."""
    return path.rstrip("/") or "/"


def _assets_router() -> Router:
    """Return the assets static-files router from the SPA handlers."""
    routers = [h for h in build_spa_route_handler() if isinstance(h, Router)]
    assert len(routers) == 1, "expected exactly one (assets) static-files router"
    return routers[0]


def test_assets_router_mounts_under_ui_path():
    """The assets static-files router is mounted at ``<ui_path>assets``.

    The SPA uses browser-history routing: the index handler serves the HTML
    (with an injected ``<base href>``) and the assets router serves the
    hashed chunks from a dedicated ``assets`` prefix.
    """
    router = _assets_router()
    expected = f"{settings.ui_path.rstrip('/')}/assets"
    assert _normalize(router.path) == _normalize(expected)


def test_index_handler_serves_ui_path_and_deep_links():
    """The index handler must claim the mount root and a multi-segment fallback.

    Registering both ``<ui_path>`` and ``<ui_path>{path:path}`` is what gives
    client-side deep links (e.g. ``/api/-/data/users``) a no-broken-reload
    guarantee under path routing.
    """
    handlers = [h for h in build_spa_route_handler() if not isinstance(h, Router)]
    assert len(handlers) == 1, "expected exactly one index route handler"
    paths = {_normalize(p) for p in handlers[0].paths}
    assert _normalize(settings.ui_path) in paths
    assert any("{path:path}" in p for p in handlers[0].paths)


def test_spa_handlers_are_excluded_from_auth():
    """Every SPA handler must opt out of auth via the ``exclude_from_auth`` key.

    The auth middleware (configured in :mod:`fusionserve.main`) honours
    Litestar's ``exclude_opt_key`` mechanism, so any handler / router carrying
    ``opt={"exclude_from_auth": True}`` is skipped without needing the URL
    pattern added to the middleware's ``exclude`` list. Losing the opt would
    silently re-enable auth on the SPA / its static assets.
    """
    for handler in build_spa_route_handler():
        assert handler.opt.get("exclude_from_auth") is True, (
            f"SPA handler {getattr(handler, 'name', handler)!r} missing exclude_from_auth opt"
        )


def test_render_index_injects_ui_path_base_href(tmp_path, monkeypatch):
    """``_render_index`` rewrites ``<base href>`` to ``settings.ui_path``."""
    import fusionserve.ui as ui_module

    bundle = tmp_path / "dist"
    bundle.mkdir()
    # Mirror the real index.html: a comment that mentions the ``<base href>``
    # element appears *before* the actual ``<base href="/" />`` tag. The
    # injection must rewrite the real tag, not the bare token in the comment.
    (bundle / "index.html").write_text(
        "<!doctype html><html><head>"
        "<!-- the ``<base href>`` element is rewritten to settings.ui_path at serve time -->"
        '<base href="/" /><title>x</title></head>'
        '<body><script type="module" src="./assets/index.js"></script></body></html>',
        encoding="utf-8",
    )
    monkeypatch.setattr(ui_module, "_BUNDLE_DIR", bundle)
    # ``_render_index`` is ``lru_cache``d on no args; clear it so it reads the
    # monkeypatched bundle (and again afterwards so the tmp result doesn't leak).
    _render_index.cache_clear()
    try:
        html = _render_index()
    finally:
        _render_index.cache_clear()

    assert f'<base href="{settings.ui_path}" />' in html
    # The real tag must be rewritten — not left as the dev-default root base
    # (which is what happens if the regex matches the comment token instead).
    assert '<base href="/" />' not in html
    # Exactly one rewritten base tag (guards against rewriting the comment).
    assert html.count(f'<base href="{settings.ui_path}"') == 1
    # Relative asset URLs are left untouched (they resolve against the base).
    assert "./assets/index.js" in html
