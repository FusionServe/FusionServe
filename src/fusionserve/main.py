from __future__ import annotations

import logging

import prometheus_client
from fastapi import FastAPI
from fastapi.concurrency import asynccontextmanager
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import PlainTextResponse

from . import graphql, rest
from .config import settings
from .persistence import introspect

_logger = logging.getLogger(settings.app_name)


swagger_ui_parameters = {
    "displayRequestDuration": True,
    "filter": True,
    "showExtensions": True,
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ---- startup ----
    Base, models_registry = introspect()
    app.include_router(rest.build(Base, models_registry))
    app.include_router(graphql.build(Base, models_registry), include_in_schema=False)
    yield


# uvicorn entry point
app = FastAPI(
    title=settings.app_name,
    openapi_url="/api/openapi.json",
    docs_url="/api/docs",
    swagger_ui_parameters=swagger_ui_parameters,
    redoc_url=None,
    redirect_slashes=False,
    lifespan=lifespan,
)

app.add_middleware(GZipMiddleware, minimum_size=1000, compresslevel=5)


@app.get("/metrics")
async def get_metrics():
    return PlainTextResponse(prometheus_client.generate_latest())
