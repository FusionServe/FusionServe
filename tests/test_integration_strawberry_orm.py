"""Integration harness for the strawberry-orm migration spike.

Brings up a disposable PostgreSQL via ``testcontainers`` seeded with a
plural parent/child pair (``authors`` / ``books``) plus an FK, a view
(``book_summaries``), real PostgreSQL roles (``app_anon`` / ``app_author``),
and a row-level-security policy on ``books`` keyed on
``current_setting('role')``. ``persistence.set_role`` issues
``set_config('role', …)`` which is equivalent to ``SET ROLE``, so the policy
sees the switched role.

Runs only when ``RUN_INTEGRATION=1`` and a Docker daemon is reachable.

Phase 1 of the spike plan validates the harness against the *current*
(strawberry-sqlalchemy) implementation as a baseline; later phases add the
read/RLS/write assertions against the strawberry-orm rewrite.
"""

from __future__ import annotations

import os
import uuid

import pytest

pytestmark = pytest.mark.integration

if os.environ.get("RUN_INTEGRATION") != "1":
    pytest.skip(
        "RUN_INTEGRATION!=1 — set RUN_INTEGRATION=1 to run docker-backed tests",
        allow_module_level=True,
    )

# Imports below the skip so the docker / testcontainers tree isn't loaded for
# unit-test runs.
from pydantic import SecretStr  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from testcontainers.postgres import PostgresContainer  # noqa: E402

#: Bearer token -> role mapping honoured by the patched auth handler. The
#: ``role`` becomes the PostgreSQL role via ``set_role`` -> ``SET ROLE``.
_TOKEN_ROLES = {"alice-token": "app_author"}


@pytest.fixture(scope="module")
def monkeypatch_module():
    """Module-scoped variant of pytest's ``monkeypatch``."""
    from _pytest.monkeypatch import MonkeyPatch

    mp = MonkeyPatch()
    yield mp
    mp.undo()


@pytest.fixture(scope="module")
def postgres_container():
    """Spin up a throwaway PostgreSQL 16 container seeded for the spike.

    Creates the ``app_public`` schema, two plural tables joined by an FK, a
    view, two NOLOGIN roles, grants, and an RLS policy on ``books`` so that
    ``app_anon`` only sees ``visibility = 'public'`` rows while ``app_author``
    sees everything.
    """
    from fusionserve.config import settings

    schema = settings.pg_app_schema
    with PostgresContainer("postgres:16-alpine") as container:
        url = container.get_connection_url().replace("postgresql+psycopg2", "postgresql+psycopg")
        engine = create_engine(url, future=True)
        try:
            with engine.begin() as conn:
                conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
                conn.execute(text("CREATE ROLE app_anon NOLOGIN"))
                conn.execute(text("CREATE ROLE app_author NOLOGIN"))
                conn.execute(text(f'GRANT USAGE ON SCHEMA "{schema}" TO app_anon, app_author'))
                conn.execute(
                    text(
                        f"""
                        CREATE TABLE "{schema}".authors (
                            id serial PRIMARY KEY,
                            name text NOT NULL
                        );
                        """
                    )
                )
                conn.execute(
                    text(
                        f"""
                        CREATE TABLE "{schema}".books (
                            id serial PRIMARY KEY,
                            author_id integer NOT NULL REFERENCES "{schema}".authors(id),
                            title text NOT NULL,
                            visibility text NOT NULL DEFAULT 'private'
                        );
                        """
                    )
                )
                conn.execute(
                    text(
                        f"""
                        CREATE VIEW "{schema}".book_summaries AS
                            SELECT b.id, b.title, a.name AS author_name
                            FROM "{schema}".books b
                            JOIN "{schema}".authors a ON a.id = b.author_id;
                        """
                    )
                )
                # Smart comment declaring the view's logical PK so introspection
                # maps it as a read-only type (undeclared views stay unmapped).
                conn.execute(
                    text(
                        f"""
                        COMMENT ON VIEW "{schema}".book_summaries IS '---
primary_key: id
---
Joined view of books and their author names.';
                        """
                    )
                )
                # Grants: app_author full CRUD, app_anon read-only.
                conn.execute(
                    text(f'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA "{schema}" TO app_author')
                )
                conn.execute(text(f'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA "{schema}" TO app_author'))
                conn.execute(
                    text(f'GRANT SELECT ON "{schema}".authors, "{schema}".books, "{schema}".book_summaries TO app_anon')
                )
                # RLS on books: anon sees only public rows; app_author sees all.
                conn.execute(text(f'ALTER TABLE "{schema}".books ENABLE ROW LEVEL SECURITY'))
                conn.execute(
                    text(
                        f"""
                        CREATE POLICY books_visibility ON "{schema}".books
                            FOR SELECT
                            USING (
                                current_setting('role', true) = 'app_author'
                                OR visibility = 'public'
                            );
                        """
                    )
                )
                # Seed rows.
                conn.execute(text(f"""INSERT INTO "{schema}".authors (id, name) VALUES (1, 'Alice'), (2, 'Bob')"""))
                conn.execute(
                    text(
                        f"""
                        INSERT INTO "{schema}".books (author_id, title, visibility) VALUES
                            (1, 'Public Alice', 'public'),
                            (1, 'Secret Alice', 'private'),
                            (2, 'Public Bob', 'public');
                        """
                    )
                )
                conn.execute(text(f"SELECT setval(pg_get_serial_sequence('\"{schema}\".authors', 'id'), 2, true)"))
        finally:
            engine.dispose()
        yield container


@pytest.fixture(scope="module")
def configured_app(postgres_container, monkeypatch_module):
    """Point ``fusionserve.*`` at the live container and stub auth.

    Rebinds the async engine/session captured by ``persistence`` and
    ``graphql`` (earlier unit tests may have imported them against an
    unreachable DSN), sets ``anonymous_role`` to the seeded ``app_anon`` role,
    and replaces ``auth.retrieve_user_handler`` with a token->role stub so
    tests can drive authenticated requests without a real JWKS endpoint.
    """
    from fusionserve.config import settings

    monkeypatch_module.setattr(settings, "pg_host", postgres_container.get_container_host_ip())
    monkeypatch_module.setattr(settings, "pg_port", int(postgres_container.get_exposed_port(5432)))
    monkeypatch_module.setattr(settings, "pg_user", postgres_container.username)
    monkeypatch_module.setattr(settings, "pg_password", SecretStr(postgres_container.password))
    monkeypatch_module.setattr(settings, "pg_database", postgres_container.dbname)
    monkeypatch_module.setattr(settings, "anonymous_role", "app_anon")

    async_url = (
        f"postgresql+asyncpg://{settings.pg_user}:{settings.pg_password.get_secret_value()}@"
        f"{settings.pg_host}:{settings.pg_port}/{settings.pg_database}"
    )
    new_engine = create_async_engine(async_url, pool_pre_ping=True)
    new_session = async_sessionmaker(new_engine, expire_on_commit=False)

    from fusionserve import auth, graphql, persistence

    monkeypatch_module.setattr(persistence, "engine", new_engine)
    monkeypatch_module.setattr(persistence, "async_session", new_session)
    monkeypatch_module.setattr(graphql, "async_session", new_session)

    async def fake_retrieve_user_handler(token: str):
        role = _TOKEN_ROLES.get(token)
        if role is None:
            return None
        return auth.User(
            id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
            username="alice",
            email="alice@example.com",
            role=role,
        )

    monkeypatch_module.setattr(auth, "retrieve_user_handler", fake_retrieve_user_handler)

    return settings


@pytest.fixture(scope="module")
def graphql_client(configured_app):
    """Yield a ``post(query, token=None)`` helper bound to a live TestClient."""
    from litestar.testing import TestClient

    from fusionserve.main import app

    base = configured_app.base_path

    with TestClient(app=app) as client:

        def post(query: str, token: str | None = None, variables: dict | None = None) -> dict:
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            payload: dict = {"query": query}
            if variables is not None:
                payload["variables"] = variables
            response = client.post(f"{base}/graphql", json=payload, headers=headers)
            assert response.status_code == 200, response.text
            return response.json()

        yield post


def test_introspect_finds_tables_and_view(configured_app):
    """``introspect()`` reflects both plural tables and classifies the view."""
    from fusionserve import persistence

    introspection = persistence.introspect()
    assert "authors" in introspection.base.classes
    assert "books" in introspection.base.classes
    assert "book_summaries" in introspection.views


def test_graphql_list_anonymous_authors(graphql_client):
    """strawberry-orm list field serves authors anonymously (native list shape)."""
    body = graphql_client("{ authors { id name } }")
    assert "errors" not in body, body
    names = {row["name"] for row in body["data"]["authors"]}
    assert {"Alice", "Bob"} <= names


def test_graphql_pk_lookup(graphql_client):
    """Primary-key lookup returns a single record."""
    body = graphql_client("{ author(id: 1) { id name } }")
    assert "errors" not in body, body
    assert body["data"]["author"]["name"] == "Alice"


def test_graphql_nested_relationship(graphql_client):
    """Authenticated nested query traverses author -> books via the optimizer.

    Note: the to-many relation field is named ``booksCollection`` (SQLAlchemy
    automap default) rather than the old implementation's singularized
    ``books`` — a documented naming-friction finding.
    """
    body = graphql_client(
        "{ authors(order: [{ field: { id: ASC } }]) { name booksCollection { title } } }",
        token="alice-token",
    )
    assert "errors" not in body, body
    authors = {a["name"]: {b["title"] for b in a["booksCollection"]} for a in body["data"]["authors"]}
    # app_author sees all of Alice's books (public + private).
    assert {"Public Alice", "Secret Alice"} <= authors["Alice"]


def test_graphql_to_one_relationship(graphql_client):
    """Traverse the to-one direction book -> author (named ``authors`` by automap)."""
    body = graphql_client(
        "{ books(order: [{ field: { id: ASC } }]) { title authors { name } } }",
        token="alice-token",
    )
    assert "errors" not in body, body
    first = body["data"]["books"][0]
    assert first["authors"]["name"] == "Alice"


def test_graphql_native_filter(graphql_client):
    """Native @oneOf filter narrows the result set."""
    body = graphql_client(
        '{ authors(filter: { field: { name: { exact: "Bob" } } }) { name } }',
    )
    assert "errors" not in body, body
    names = [row["name"] for row in body["data"]["authors"]]
    assert names == ["Bob"]


def test_rls_anonymous_sees_only_public_books(graphql_client):
    """RLS: an anonymous request (app_anon role) sees only public books."""
    body = graphql_client("{ books { title visibility } }")
    assert "errors" not in body, body
    titles = {row["title"] for row in body["data"]["books"]}
    assert titles == {"Public Alice", "Public Bob"}
    assert "Secret Alice" not in titles


def test_rls_authenticated_sees_all_books(graphql_client):
    """RLS: an authenticated request (app_author role) sees private books too."""
    body = graphql_client("{ books { title } }", token="alice-token")
    assert "errors" not in body, body
    titles = {row["title"] for row in body["data"]["books"]}
    assert "Secret Alice" in titles


def test_rls_nested_anonymous_does_not_leak(graphql_client):
    """RLS holds through a nested relation load, not just the root query.

    The strawberry-orm docs warn that parent scoping does not flow to children;
    here the role-scoped session must make the nested books load honour RLS too.
    """
    body = graphql_client("{ authors { name booksCollection { title } } }")
    assert "errors" not in body, body
    by_author = {a["name"]: {b["title"] for b in a["booksCollection"]} for a in body["data"]["authors"]}
    # Anonymous: Alice's nested books exclude the private one.
    assert "Secret Alice" not in by_author.get("Alice", set())
    assert "Public Alice" in by_author.get("Alice", set())


def test_mutation_create_and_delete(graphql_client):
    """create<Singular> then delete<Singular> by primary key (RETURNING-based)."""
    body = graphql_client('mutation { createAuthor(input: { name: "Carol" }) { id name } }', token="alice-token")
    assert "errors" not in body, body
    created = body["data"]["createAuthor"]
    assert created["name"] == "Carol"
    new_id = created["id"]

    body = graphql_client(f"mutation {{ deleteAuthor(id: {new_id}) {{ name }} }}", token="alice-token")
    assert "errors" not in body, body
    assert body["data"]["deleteAuthor"]["name"] == "Carol"


def test_mutation_create_many_and_delete_many(graphql_client):
    """create<Plural> inserts many; delete<Plural> removes them via a where filter."""
    body = graphql_client(
        'mutation { createAuthors(inputs: [{ name: "D1" }, { name: "D2" }]) { id name } }',
        token="alice-token",
    )
    assert "errors" not in body, body
    names = {r["name"] for r in body["data"]["createAuthors"]}
    assert {"D1", "D2"} == names

    body = graphql_client(
        'mutation { deleteAuthors(where: { field: { name: { inList: ["D1", "D2"] } } }) { name } }',
        token="alice-token",
    )
    assert "errors" not in body, body
    assert {r["name"] for r in body["data"]["deleteAuthors"]} == {"D1", "D2"}


def test_mutation_update_by_pk(graphql_client):
    """update<Singular> patches a single record by primary key."""
    created = graphql_client('mutation { createAuthor(input: { name: "ToRename" }) { id } }', token="alice-token")
    aid = created["data"]["createAuthor"]["id"]
    try:
        body = graphql_client(
            f'mutation {{ updateAuthor(id: {aid}, patch: {{ name: "Renamed" }}) {{ name }} }}',
            token="alice-token",
        )
        assert "errors" not in body, body
        assert body["data"]["updateAuthor"]["name"] == "Renamed"
    finally:
        graphql_client(f"mutation {{ deleteAuthor(id: {aid}) {{ id }} }}", token="alice-token")


def test_mutation_update_many_empty_where_guardrail(graphql_client):
    """update<Plural> with a filter that resolves to no condition is rejected."""
    body = graphql_client(
        'mutation { updateAuthors(patch: { name: "X" }, where: { all: [] }) { id } }',
        token="alice-token",
    )
    assert "errors" in body, body
    assert "where must contain" in body["errors"][0]["message"]


def test_mutation_delete_many_empty_where_guardrail(graphql_client):
    """delete<Plural> with a filter that resolves to no condition is rejected."""
    body = graphql_client(
        "mutation { deleteAuthors(where: { all: [] }) { id } }",
        token="alice-token",
    )
    assert "errors" in body, body
    assert "where must contain" in body["errors"][0]["message"]


def test_graphql_nested_relationship_is_bounded(configured_app, graphql_client):
    """Nested author->books query issues a bounded number of SELECTs (no N+1)."""
    from sqlalchemy import event

    from fusionserve import persistence

    selects: list[str] = []

    def _count(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT") and "FROM" in statement.upper():
            selects.append(statement)

    sync_engine = persistence.engine.sync_engine
    event.listen(sync_engine, "after_cursor_execute", _count)
    try:
        body = graphql_client("{ authors { name booksCollection { title } } }", token="alice-token")
    finally:
        event.remove(sync_engine, "after_cursor_execute", _count)
    assert "errors" not in body, body
    # 2 seeded authors; a per-row (N+1) load would scale with author count.
    # Expect ~2 SELECTs (authors, then a single batched books load) plus minor
    # overhead — bounded well under a per-author count.
    assert len(selects) <= 4, f"unexpected SELECT count {len(selects)}: {selects}"
