from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from litestar import Litestar

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
    # app.include_router(rest.build(Base, models_registry))
    # app.include_router(graphql.build(Base, models_registry), include_in_schema=False)
    for controller in rest.build_controllers(Base, models_registry):
        app.register(controller)
    yield


app = Litestar(
    route_handlers=[],
    # openapi_config={"openapi_url": "/api/openapi.json", "docs_url": "/api/docs"},
    lifespan=[lifespan],
)

# app.add_middleware(GZipMiddleware, minimum_size=1000, compresslevel=5)


# @app.get("/metrics")
# async def get_metrics():
#    return PlainTextResponse(prometheus_client.generate_latest())
