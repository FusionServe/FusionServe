"""UI integration for FusionServe.

This module wires the React SPA built under ``ui/`` into the Litestar
application via :mod:`litestar_vite`. Two artefacts are exported:

- :class:`RedirectRenderPlugin` — a custom
  :class:`~litestar.openapi.plugins.OpenAPIRenderPlugin` registered as
  the default plugin on the OpenAPI router. The upstream OpenAPI plugin
  auto-mounts the default plugin at the configured router root, so
  ``GET /api/`` returns a 302 redirect to :attr:`Settings.ui_path`
  (default ``/-/``) where the SPA is mounted. Sibling render plugins
  (``SwaggerRenderPlugin``, ``ScalarRenderPlugin``) coexist normally at
  ``/api/swagger`` and ``/api/scalar``.
- :func:`build_vite_plugin` — the :class:`VitePlugin` constructor used
  by :mod:`fusionserve.main`. Configures SPA mode with ``spa_path``
  pinned to :attr:`Settings.ui_path` and the asset URL pinned to
  :attr:`Settings.ui_assets_path` (default ``/-/assets/``). The bundled
  build is packaged inside the Python wheel under
  ``src/fusionserve/web/dist``.

Path constraints worth knowing:

- ``Settings.ui_assets_path`` must stay **outside**
  ``Settings.base_path`` because the OpenAPI router (mounted at
  ``base_path``) registers a ``<base_path>/{path:str}`` not-found
  handler that would otherwise shadow asset requests. The same literal
  is hard-coded in ``ui/vite.config.ts``; the two must match.
- ``Settings.ui_path`` doubles as the ``spa_path`` value passed to
  ``litestar-vite`` (which registers ``<ui_path>/`` and
  ``<ui_path>/{path:path}`` SPA routes) AND the target of the
  ``/api/`` -> SPA redirect.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from litestar.connection import Request
from litestar.enums import MediaType, OpenAPIMediaType
from litestar.openapi.plugins import OpenAPIRenderPlugin
from litestar.response import Redirect
from litestar_vite import PathConfig, RuntimeConfig, ViteConfig, VitePlugin

from .config import settings

#: Repository root, derived from this file's location. Layout assumed:
#: ``<repo>/src/fusionserve/ui.py``. Used to resolve the ``ui/`` source
#: directory and the bundled ``web/dist`` output directory.
_PACKAGE_DIR: Path = Path(__file__).resolve().parent
_REPO_ROOT: Path = _PACKAGE_DIR.parent.parent

#: Where the production Vite build emits assets. Co-located with the
#: Python package so the wheel ships the built SPA without extra
#: packaging configuration.
_BUNDLE_DIR: Path = _PACKAGE_DIR / "web" / "dist"

#: TypeScript / React source directory. Only consulted in dev mode.
_RESOURCE_DIR: Path = _REPO_ROOT / "ui" / "src"

#: Public static directory for source-side assets (favicon, etc.).
_STATIC_DIR: Path = _REPO_ROOT / "ui" / "public"


class RedirectRenderPlugin(OpenAPIRenderPlugin):
    """OpenAPI render plugin that redirects to the React SPA.

    Registered as the first entry of
    :attr:`~litestar.openapi.OpenAPIConfig.render_plugins`, this plugin
    becomes the *default* plugin. Litestar's OpenAPI router auto-mounts
    the default plugin's handler at the router root (``settings.base_path``
    in our setup), so ``GET /api/`` returns a 302 redirect to
    :attr:`Settings.ui_path` — where ``litestar-vite`` serves the SPA via
    its root catch-all. Users hitting the canonical API base URL land in
    the UI without having to know the SPA path.

    The plugin departs from the sibling render plugins
    (``JsonRenderPlugin``, ``SwaggerRenderPlugin``, …) in one important
    way: :meth:`render` returns a :class:`~litestar.response.Redirect`
    Response rather than ``bytes``. Litestar handles ``Response`` returns
    from handlers regardless of the declared return annotation, so the
    302 is served directly; the upstream ``-> bytes`` signature is kept
    only for API parity with the rest of the render-plugin family.
    """

    def __init__(
        self,
        *,
        path: str | Sequence[str] = "",
        media_type: MediaType | OpenAPIMediaType = MediaType.TEXT,
        **kwargs: Any,
    ) -> None:
        """Initialise the redirect plugin.

        Args:
            path: Path (relative to the OpenAPI router root) at which the
                redirect handler is registered. Defaults to ``""`` so the
                handler binds to the router root itself — the upstream
                OpenAPI plugin additionally promotes the default plugin's
                handler to ``"/"``, which together means ``/api`` and
                ``/api/`` both redirect.
            media_type: Required by the
                :class:`OpenAPIRenderPlugin` base class but practically
                ignored: the :class:`Redirect` Response sets its own
                ``Location`` header and ``302`` status independently of
                the declared media type.
            **kwargs: Forwarded to the base class for forward-compat with
                future :class:`OpenAPIRenderPlugin` parameters.
        """
        super().__init__(path=path, media_type=media_type, **kwargs)

    def render(self, request: Request, openapi_schema: dict[str, Any]) -> bytes:
        """Return a 302 redirect to :attr:`Settings.ui_path`.

        The return type annotation is ``bytes`` for parity with the
        :class:`OpenAPIRenderPlugin` contract, but the actual return
        value is a :class:`~litestar.response.Redirect` Response.
        Litestar's response cycle accepts any ``Response`` instance from
        a handler return — it short-circuits serialization and sends the
        response as-is — so the upstream ``-> bytes`` signature is a
        documentation contract rather than a runtime constraint here.

        Args:
            request: The incoming request. Unused (the redirect target
                is configuration-driven, not request-derived) but kept
                in the signature to match the base class.
            openapi_schema: The fully-generated OpenAPI schema. Unused
                because we never render OpenAPI HTML from this plugin.

        Returns:
            A :class:`Redirect` Response pointing at
            :attr:`Settings.ui_path` with status ``302``. Typed as
            ``bytes`` to match the upstream contract — see the
            class-level note above.
        """
        return Redirect(path=settings.ui_path, status_code=302, media_type=self.media_type)


def build_vite_plugin() -> VitePlugin:
    """Construct the :class:`VitePlugin` used by the Litestar app.

    SPA mode with the bundle directory pinned to
    ``src/fusionserve/web/dist`` so the build artefact ships inside the
    Python wheel. The SPA is mounted at
    :attr:`Settings.ui_path` — ``litestar-vite``'s ``AppHandler``
    registers a literal ``<ui_path>/`` route plus a
    ``<ui_path>/{path:path}`` catch-all so any deep URL inside the SPA
    prefix resolves to ``index.html`` (with client-side hash routing
    handling navigation inside the SPA).

    Two configuration knobs come from :class:`fusionserve.config.Settings`:

    - ``spa_path=settings.ui_path`` (default ``/-/``) — where Litestar
      registers the SPA routes. Users typically reach it via the
      ``/api/`` → ``ui_path`` 302 emitted by
      :class:`RedirectRenderPlugin`, so the literal value is
      configurable without breaking discovery.
    - ``asset_url=settings.ui_assets_path`` (default ``/-/assets/``) —
      the URL prefix Vite uses for hashed JS/CSS chunks. **Must stay
      outside** :attr:`Settings.base_path`: the OpenAPI router mounted
      at ``base_path`` auto-registers a ``<base_path>/{path:str}``
      not-found handler that would otherwise shadow asset requests.
      The matching literal is hard-coded in ``ui/vite.config.ts``; the
      two values must stay in sync. Litestar's router prefers the
      more-specific ``<ui_assets_path>/{file_path:path}`` static
      handler over the SPA's ``<ui_path>/{path:path}`` catch-all, so
      hosting assets *inside* ``ui_path`` (i.e. ``/-/assets/`` under
      ``/-/``) does not cause shadowing.

    The Vite dev server is launched via :command:`bun` when
    ``settings.vite_dev_mode`` is true; production builds use the
    pre-rendered ``index.html`` + manifest under the bundle dir
    (regenerated by ``bun run build`` in ``ui/``).

    Returns:
        A configured :class:`VitePlugin` ready to be passed to
        :class:`litestar.Litestar` via ``plugins=[...]``.
    """
    return VitePlugin(
        config=ViteConfig(
            mode="spa",
            dev_mode=settings.vite_dev_mode,
            spa_path=settings.ui_path,
            paths=PathConfig(
                root=_REPO_ROOT / "ui",
                bundle_dir=_BUNDLE_DIR,
                resource_dir=_RESOURCE_DIR,
                static_dir=_STATIC_DIR,
                asset_url=settings.ui_assets_path,
            ),
            runtime=RuntimeConfig(executor="bun", is_react=True),
        ),
    )


__all__ = ("RedirectRenderPlugin", "build_vite_plugin")
