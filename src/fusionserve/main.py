from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from litestar import Litestar
from litestar.config.compression import CompressionConfig
from litestar.openapi import OpenAPIConfig
from litestar.openapi.plugins import ScalarRenderPlugin, SwaggerRenderPlugin
from litestar.plugins.prometheus import PrometheusConfig, PrometheusController

from . import rest
from .config import settings
from .persistence import introspect

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
    yield


app = Litestar(
    route_handlers=[PrometheusController],
    lifespan=[lifespan],
    debug=settings.debug,
    openapi_config=OpenAPIConfig(
        title=settings.app_name,
        version="1.0.0",
        path="/api/docs",
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
    middleware=[PrometheusConfig(group_path=True, labels={"metrics": "get"}).middleware],
)
