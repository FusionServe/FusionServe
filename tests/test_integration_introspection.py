"""Integration tests: bring up a disposable PostgreSQL and exercise startup.

Runs only when ``RUN_INTEGRATION=1`` is set and a Docker daemon is reachable.
The fixtures stand up a fresh PostgreSQL container via ``testcontainers``,
seed a tiny plural-named schema, point the application config at it, and
exercise both ``persistence.introspect()`` and a single REST round-trip via
Litestar's ``TestClient``.

The fixtures live in this file (rather than ``conftest.py``) so the
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

# Imports below are intentionally below the module-level skip so the docker
# / testcontainers dependency tree isn't loaded for unit-test runs.
from pydantic import SecretStr  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from testcontainers.postgres import PostgresContainer  # noqa: E402


@pytest.fixture(scope="module")
def monkeypatch_module():
    """Module-scoped variant of pytest's monkeypatch."""
    from _pytest.monkeypatch import MonkeyPatch

    mp = MonkeyPatch()
    yield mp
    mp.undo()


@pytest.fixture(scope="module")
def postgres_container():
    """Spin up a throwaway PostgreSQL 16 container for the module.

    Seeds a single plural-named table inside the configured app schema
    (``app_public`` by default). The introspection contract rejects
    singularly-named tables, and ``introspect()`` issues
    ``CREATE OR REPLACE FUNCTION <schema>.current_user_id()`` against the
    same schema, so the schema must already exist and be writable by the
    seeded user.
    """
    from fusionserve.config import settings

    schema = settings.pg_app_schema
    with PostgresContainer("postgres:16-alpine") as container:
        url = container.get_connection_url().replace("postgresql+psycopg2", "postgresql+psycopg")
        engine = create_engine(url, future=True)
        try:
            with engine.begin() as conn:
                conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
                conn.execute(
                    text(
                        f"""
                        CREATE TABLE IF NOT EXISTS "{schema}".widgets (
                            id serial PRIMARY KEY,
                            name text NOT NULL,
                            created_at timestamptz NOT NULL DEFAULT now()
                        );
                        """
                    )
                )
                conn.execute(text(f"""INSERT INTO "{schema}".widgets (name) VALUES ('alpha'), ('beta')"""))
        finally:
            engine.dispose()
        yield container


@pytest.fixture(scope="module")
def configured_app(postgres_container, monkeypatch_module):
    """Patch ``fusionserve.*`` so it points at the live container.

    Settings are pinned to the production-shaped defaults (``pg_app_schema =
    "app_public"``, etc.), and the module-level ``engine`` / ``async_session``
    objects in ``persistence`` and ``graphql`` are rebuilt against the new
    DSN. These rebinds matter because earlier unit tests in the same pytest
    process may have already imported ``persistence`` and ``graphql``,
    capturing the original (unreachable) engine.

    Yields the configured ``settings`` instance.
    """
    from fusionserve.config import settings

    monkeypatch_module.setattr(settings, "pg_host", postgres_container.get_container_host_ip())
    monkeypatch_module.setattr(settings, "pg_port", int(postgres_container.get_exposed_port(5432)))
    monkeypatch_module.setattr(settings, "pg_user", postgres_container.username)
    monkeypatch_module.setattr(settings, "pg_password", SecretStr(postgres_container.password))
    monkeypatch_module.setattr(settings, "pg_database", postgres_container.dbname)
    # `anonymous_role` must be an existing PostgreSQL role; the container's
    # superuser is the safest default for a smoke test.
    monkeypatch_module.setattr(settings, "anonymous_role", postgres_container.username)

    async_url = (
        f"postgresql+asyncpg://{settings.pg_user}:{settings.pg_password.get_secret_value()}@"
        f"{settings.pg_host}:{settings.pg_port}/{settings.pg_database}"
    )
    new_engine = create_async_engine(async_url, pool_pre_ping=True)
    new_session = async_sessionmaker(new_engine, expire_on_commit=False)

    # Rebind every module that captured `engine` / `async_session` by value.
    from fusionserve import graphql, persistence

    monkeypatch_module.setattr(persistence, "engine", new_engine)
    monkeypatch_module.setattr(persistence, "async_session", new_session)
    monkeypatch_module.setattr(graphql, "async_session", new_session)

    return settings


def test_introspect_succeeds_against_live_db(configured_app):
    """``introspect()`` reflects the seeded schema and returns automap classes."""
    from fusionserve import persistence

    base = persistence.introspect()
    assert "widgets" in base.classes


def test_rest_list_endpoint_returns_seeded_rows(configured_app):
    """End-to-end: build the app, mount controllers, hit GET /api/widgets."""
    from litestar.testing import TestClient

    # Import is deferred so the lifespan picks up the patched persistence
    # bindings rather than the originals.
    from fusionserve.main import app

    with TestClient(app=app) as client:
        response = client.get(f"{configured_app.base_path}/widgets")
        assert response.status_code == 200, response.text
        body = response.json()
        names = {row["name"] for row in body}
        assert {"alpha", "beta"} <= names
