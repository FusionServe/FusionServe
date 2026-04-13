from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

from litestar import Litestar
from litestar.config.compression import CompressionConfig
from litestar.connection import ASGIConnection
from litestar.di import Provide
from litestar.middleware import AbstractAuthenticationMiddleware, AuthenticationResult, DefineMiddleware
from litestar.openapi import OpenAPIConfig
from litestar.openapi.plugins import ScalarRenderPlugin, SwaggerRenderPlugin
from litestar.plugins.prometheus import PrometheusConfig, PrometheusController
from litestar.security.jwt import Token
from pydantic import BaseModel

from . import graphql, rest
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
    # app.include_router(graphql.build(Base, models_registry), include_in_schema=False)
    for controller in rest.build_controllers(Base, models_registry):
        app.register(controller)
    app.register(graphql.build(Base, models_registry))
    yield


class User(BaseModel):
    id: UUID
    name: str


async def retrieve_user_handler(token: Token, connection: ASGIConnection[Any, Any, Any, Any]) -> User | None:
    # TODO: Implement logic here to retrieve the user instance
    return User(id=UUID("12345678-1234-5678-1234-567812345678"), name="John Doe")


class AuthMiddleware(AbstractAuthenticationMiddleware):
    async def authenticate_request(self, connection: ASGIConnection) -> AuthenticationResult:
        """Given a request, parse the header and retrieve the user from the token"""

        # retrieve the auth header
        auth_header = connection.headers.get("Authorization")
        if not auth_header:
            return AuthenticationResult(user=None, auth=None)

        return AuthenticationResult(
            user=await retrieve_user_handler(auth_header, connection),
            auth=auth_header,
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
    ),
    compression_config=CompressionConfig(backend="brotli", brotli_gzip_fallback=True),
    middleware=[PrometheusConfig(group_path=True, labels={"metrics": "get"}).middleware, auth_mw],
    dependencies={"session": Provide(get_async_session)},
)
