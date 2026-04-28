"""Integration test: bring up a disposable PostgreSQL and exercise startup.

Runs only when ``RUN_INTEGRATION=1`` is set and a Docker daemon is reachable.
The test stands up a fresh PostgreSQL container via ``testcontainers``,
seeds a tiny plural-named schema, points the application config at it,
runs ``persistence.introspect()`` and a single REST round-trip via
Litestar's ``TestClient``.

The fixtures here live in this file (rather than ``conftest.py``) so the
container is only started for tests in this module — unit tests stay fast.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.integration

if os.environ.get("RUN_INTEGRATION") != "1":
    pytest.skip(
        "RUN_INTEGRATION!=1 — set RUN_INTEGRATION=1 to run docker-backed tests",
        allow_module_level=True,
    )

from sqlalchemy import create_engine, text  # noqa: E402  (skip-or-import order)
from testcontainers.postgres import PostgresContainer  # noqa: E402


@pytest.fixture(scope="module")
def postgres_container():
    """Spin up a throwaway PostgreSQL 16 container for the module."""
    with PostgresContainer("postgres:16-alpine") as container:
        # Seed: create a plural-named table in the default schema (``public``)
        # because the app contract requires plural names.
        url = container.get_connection_url().replace("postgresql+psycopg2", "postgresql+psycopg")
        engine = create_engine(url, future=True)
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS public.widgets (
                        id serial PRIMARY KEY,
                        name text NOT NULL,
                        created_at timestamptz NOT NULL DEFAULT now()
                    );
                    """
                )
            )
            conn.execute(text("INSERT INTO public.widgets (name) VALUES ('alpha'), ('beta')"))
        engine.dispose()
        yield container


@pytest.fixture(scope="module")
def configured_settings(postgres_container, monkeypatch_module):
    """Point the app's settings at the live container."""
    from fusionserve.config import settings

    monkeypatch_module.setattr(settings, "pg_host", postgres_container.get_container_host_ip())
    monkeypatch_module.setattr(settings, "pg_port", int(postgres_container.get_exposed_port(5432)))
    monkeypatch_module.setattr(settings, "pg_user", postgres_container.username)
    from pydantic import SecretStr

    monkeypatch_module.setattr(settings, "pg_password", SecretStr(postgres_container.password))
    monkeypatch_module.setattr(settings, "pg_database", postgres_container.dbname)
    monkeypatch_module.setattr(settings, "pg_app_schema", "public")
    return settings


@pytest.fixture(scope="module")
def monkeypatch_module():
    """Module-scoped variant of pytest's monkeypatch."""
    from _pytest.monkeypatch import MonkeyPatch

    mp = MonkeyPatch()
    yield mp
    mp.undo()


def test_introspect_succeeds_against_live_db(configured_settings):
    """``introspect()`` reflects the seeded schema and returns automap classes."""
    # Re-create the async engine so it picks up the patched settings.
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from fusionserve import persistence

    async_url = (
        f"postgresql+asyncpg://{configured_settings.pg_user}:"
        f"{configured_settings.pg_password.get_secret_value()}@"
        f"{configured_settings.pg_host}:{configured_settings.pg_port}/"
        f"{configured_settings.pg_database}"
    )
    persistence.engine = create_async_engine(async_url, pool_pre_ping=True)
    persistence.async_session = async_sessionmaker(persistence.engine, expire_on_commit=False)

    base = persistence.introspect()
    assert "widgets" in base.classes


def test_rest_list_endpoint_returns_seeded_rows(configured_settings):
    """End-to-end: build the app, mount controllers, hit GET /api/widgets."""
    from litestar.testing import TestClient

    # Importing ``fusionserve.main`` triggers the lifespan on TestClient enter.
    from fusionserve.main import app

    with TestClient(app=app) as client:
        response = client.get(f"{configured_settings.base_path}/widgets")
        assert response.status_code == 200, response.text
        body = response.json()
        names = {row["name"] for row in body}
        assert {"alpha", "beta"} <= names
