from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from litestar import Litestar
from litestar.config.compression import CompressionConfig
from litestar.connection import ASGIConnection
from litestar.di import Provide
from litestar.middleware import AbstractAuthenticationMiddleware, AuthenticationResult, DefineMiddleware
from litestar.openapi import OpenAPIConfig
from litestar.openapi.plugins import ScalarRenderPlugin, SwaggerRenderPlugin
from litestar.openapi.spec import Components, SecurityScheme
from litestar.plugins.prometheus import PrometheusConfig, PrometheusController

from . import auth, graphql, rest
from .config import settings
from .persistence import get_async_session, introspect

_logger = logging.getLogger(settings.app_name)


swagger_ui_parameters = {
    "displayRequestDuration": True,
    "filter": True,
    "showExtensions": True,
}


@asynccontextmanager
async def lifespan(app: Litestar):
    # ---- startup ----
    Base, models_registry = introspect()
    for controller in rest.build(Base, models_registry):
        app.register(controller)
    app.register(graphql.build(Base))
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


auth_mw = DefineMiddleware(AuthMiddleware, exclude="/metrics")

app = Litestar(
    route_handlers=[PrometheusController],
    lifespan=[lifespan],
    debug=settings.debug,
    openapi_config=OpenAPIConfig(
        title=settings.app_name,
        version="1.0.0",
        path=f"{settings.base_path}/docs",
        root_schema_site="swagger",
        render_plugins=[
            SwaggerRenderPlugin(),
            ScalarRenderPlugin(
                options={
                    "theme": "elysiajs",
                    "defaultOpenFirstTag": False,
                    "darkMode": True,
                }
            ),
        ],
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
