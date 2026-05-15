"""Unit tests for ``fusionserve.ui``.

The module surface is intentionally tiny — a :class:`RedirectRenderPlugin`
that diverts ``/api/`` traffic to the React SPA, plus a
:func:`build_vite_plugin` factory. The test below pins the placement of
the Vite asset prefix so a future refactor cannot accidentally move it
inside ``settings.base_path`` (which would let the OpenAPI router shadow
asset requests).
"""

from __future__ import annotations

from fusionserve.config import settings
from fusionserve.ui import build_vite_plugin


def test_build_vite_plugin_uses_configured_asset_url():
    """The Vite plugin must serve assets outside ``settings.base_path``.

    The OpenAPI router (mounted at ``settings.base_path``) registers a
    not-found handler at ``<base_path>/{path:str}`` plus an auto-generated
    ``<base_path>/openapi.json`` handler; any asset URL that lives under
    ``base_path`` is therefore at risk of being shadowed by one of those.
    The asset URL is wired through :attr:`Settings.ui_assets_path` and
    must remain a sibling top-level prefix.
    """
    plugin = build_vite_plugin()

    # PathConfig.__post_init__ auto-appends a trailing slash when the
    # configured value doesn't already end with one. Mirror that here so
    # the assertion doesn't break the day someone normalises the default.
    expected = settings.ui_assets_path.rstrip("/") + "/"
    assert plugin.config.asset_url == expected
    assert not plugin.config.asset_url.startswith(settings.base_path.rstrip("/") + "/")
