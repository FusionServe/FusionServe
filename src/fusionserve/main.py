from __future__ import annotations

import logging
import os

import prometheus_client
from fastapi import FastAPI
from fastapi.concurrency import asynccontextmanager
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import PlainTextResponse

from . import graphql
from .config import settings
from .rest import add_routes

_logger = logging.getLogger("uvicorn.error")
_logger.setLevel(os.environ.get("LOG_LEVEL", "ERROR"))

swagger_ui_parameters = {
    "displayRequestDuration": True,
    "filter": True,
    "showExtensions": True,
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ---- startup ----
    add_routes(app)
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

app.include_router(graphql.router, prefix="/graphql")


@app.get("/metrics")
async def get_metrics():
    return PlainTextResponse(prometheus_client.generate_latest())
