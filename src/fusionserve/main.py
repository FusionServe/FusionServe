from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from litestar import Litestar
from litestar.config.compression import CompressionConfig
from litestar.connection import ASGIConnection
from litestar.di import Provide
from litestar.middleware import AbstractAuthenticationMiddleware, AuthenticationResult, DefineMiddleware
from litestar.openapi import OpenAPIConfig
from litestar.openapi.plugins import JsonRenderPlugin, ScalarRenderPlugin, SwaggerRenderPlugin
from litestar.openapi.spec import Components, SecurityScheme
from litestar.plugins.prometheus import PrometheusConfig, PrometheusController

from . import auth, files, graphql, rest, ui
from .config import get_config, settings
from .persistence import get_async_session, introspect
from .storage import get_storage

_logger = logging.getLogger(settings.app_name)


@asynccontextmanager
async def lifespan(app: Litestar):
    # ---- startup ----
    introspection = introspect()
    for controller in rest.build(introspection):
        app.register(controller)
    for controller in rest.build_function_controllers(introspection):
        app.register(controller)

    app.register(graphql.build(introspection))
    # ---- file uploads ----
    # The files controller mounts at ``<base_path>/v1/_uploads`` and
    # coexists with the auto-generated CRUD for the ``uploads`` table
    # at ``<base_path>/v1/uploads``. Operators are expected to lock
    # the auto-generated routes down via RLS (see docs/files.md). The
    # feature gracefully disables when the operator-supplied table is
    # absent — the rest of the app still starts.
    uploads_cls = introspection.base.classes.get(settings.storage_metadata_table)
    if uploads_cls is not None:
        files.validate_uploads_table(uploads_cls)
        app.register(files.build_controller(uploads_cls, get_storage()))
        _logger.info("File uploads enabled at %s/v1/_uploads", settings.base_path)
    else:
        _logger.warning(
            "File uploads disabled: table %s.%s not found.",
            settings.pg_app_schema,
            settings.storage_metadata_table,
        )
    yield


class AuthMiddleware(AbstractAuthenticationMiddleware):
    async def authenticate_request(self, connection: ASGIConnection) -> AuthenticationResult:
        """Given a request, parse the Authorization header and retrieve the user from the JWT."""

        auth_header = connection.headers.get("Authorization")

        if not auth_header:
            return AuthenticationResult(user=None, auth=None)

        # Require Bearer scheme; ignore other schemes silently
        if not auth_header.startswith("Bearer "):
            return AuthenticationResult(user=None, auth=None)

        token = auth_header.removeprefix("Bearer ")
        return AuthenticationResult(
            user=await auth.retrieve_user_handler(token),
            auth=token,
        )


# Auth-middleware exclusions: ``/metrics`` and the OpenAPI surfaces.
# The SPA handlers built by ``ui.build_spa_route_handler`` each carry
# ``opt={"exclude_from_auth": True}`` so the middleware skips them via
# its ``exclude_opt_key`` mechanism — no URL patterns required here.
auth_mw = DefineMiddleware(
    AuthMiddleware,
    exclude=["/metrics", "/api/openapi.json"],
)


route_handlers = [PrometheusController, get_config]
if settings.ui_enabled:
    route_handlers.extend(ui.build_spa_route_handler())


app = Litestar(
    route_handlers=route_handlers,
    lifespan=[lifespan],
    debug=settings.debug,
    openapi_config=OpenAPIConfig(
        title=settings.app_name,
        version="1.0.0",
        path=f"{settings.base_path}",
        render_plugins=[
            ui.RedirectRenderPlugin(),
            SwaggerRenderPlugin(),
            ScalarRenderPlugin(
                options={
                    "theme": "elysiajs",
                    "defaultOpenFirstTag": False,
                    "darkMode": True,
                }
            ),
        ]
        if settings.ui_enabled
        else [JsonRenderPlugin()],
        components=Components(
            security_schemes={
                "BearerToken": SecurityScheme(
                    type="http",
                    scheme="bearer",
                )
            },
        ),
    ),
    compression_config=CompressionConfig(backend="brotli", brotli_gzip_fallback=True),
    middleware=[PrometheusConfig(group_path=True, labels={"metrics": "get"}).middleware, auth_mw],
    dependencies={"session": Provide(get_async_session)},
)
