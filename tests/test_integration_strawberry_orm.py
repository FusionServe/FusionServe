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


def _edge_nodes(connection: dict) -> list[dict]:
    """Extract node dicts from a connection's ``edges``."""
    return [edge["node"] for edge in connection["edges"]]


def _decode_cursor(cursor: str) -> str:
    """Decode a connection cursor to its ``<Type>:<pk|pk>`` string form."""
    import base64

    return base64.b64decode(cursor).decode()


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
                # app_writer can write but, like anon, only *reads* public books
                # (RLS policy privileges app_author only) — used to prove the
                # role survives a mutation's post-commit nested loads.
                conn.execute(text("CREATE ROLE app_writer NOLOGIN"))
                conn.execute(text(f'GRANT USAGE ON SCHEMA "{schema}" TO app_anon, app_author, app_writer'))
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
                            visibility text NOT NULL DEFAULT 'private',
                            attributes jsonb
                        );
                        """
                    )
                )
                # Composite-PK table to exercise composite cursors / keyset.
                conn.execute(
                    text(
                        f"""
                        CREATE TABLE "{schema}".book_tags (
                            book_id integer NOT NULL REFERENCES "{schema}".books(id),
                            tag text NOT NULL,
                            PRIMARY KEY (book_id, tag)
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
                conn.execute(text(f"COMMENT ON COLUMN \"{schema}\".books.title IS 'The book title.'"))
                # STABLE functions for the custom-query (PG-function) surface.
                conn.execute(
                    text(
                        f'CREATE FUNCTION "{schema}".public_books() RETURNS SETOF "{schema}".books '
                        f"LANGUAGE sql STABLE AS $$ SELECT * FROM \"{schema}\".books WHERE visibility = 'public' $$"
                    )
                )
                conn.execute(
                    text(
                        f'CREATE FUNCTION "{schema}".book_count() RETURNS integer '
                        f'LANGUAGE sql STABLE AS $$ SELECT count(*)::int FROM "{schema}".books $$'
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
                # Grants: app_author + app_writer full CRUD, app_anon read-only.
                conn.execute(
                    text(
                        f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES "
                        f'IN SCHEMA "{schema}" TO app_author, app_writer'
                    )
                )
                conn.execute(
                    text(f'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA "{schema}" TO app_author, app_writer')
                )
                conn.execute(
                    text(
                        f'GRANT SELECT ON "{schema}".authors, "{schema}".books, '
                        f'"{schema}".book_tags, "{schema}".book_summaries TO app_anon'
                    )
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
                        INSERT INTO "{schema}".books (author_id, title, visibility, attributes) VALUES
                            (1, 'Public Alice', 'public', '{{"genre": "fiction"}}'),
                            (1, 'Secret Alice', 'private', NULL),
                            (2, 'Public Bob', 'public', NULL);
                        """
                    )
                )
                conn.execute(
                    text(
                        f"""
                        INSERT INTO "{schema}".book_tags (book_id, tag) VALUES
                            (1, 'classic'), (1, 'fiction'), (3, 'humor');
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


def test_connection_edges_and_nodes_shapes(graphql_client):
    """The connection exposes both edges{cursor node} and a flat nodes list."""
    body = graphql_client("{ authors { totalCount edges { cursor node { name } } nodes { name } } }")
    assert "errors" not in body, body
    conn = body["data"]["authors"]
    assert conn["totalCount"] == 2
    assert {n["name"] for n in _edge_nodes(conn)} == {"Alice", "Bob"}
    assert {n["name"] for n in conn["nodes"]} == {"Alice", "Bob"}
    assert all(e["cursor"] for e in conn["edges"])


def test_connection_native_id_is_raw(graphql_client):
    """Native PK columns are visible as raw values (no relay GlobalID)."""
    body = graphql_client("{ authors(order: [{ field: { id: ASC } }]) { edges { node { id name } } } }")
    assert "errors" not in body, body
    ids = [n["id"] for n in _edge_nodes(body["data"]["authors"])]
    assert ids == [1, 2]  # raw integers, not opaque global ids


def test_connection_cursor_format(graphql_client):
    """Cursor is base64('<Type>:<pk|pk>') when ordering defaults to the PK."""
    body = graphql_client("{ authors(order: [{ field: { id: ASC } }]) { edges { cursor node { id } } } }")
    assert "errors" not in body, body
    edges = body["data"]["authors"]["edges"]
    assert _decode_cursor(edges[0]["cursor"]) == "Author:1"
    assert _decode_cursor(edges[1]["cursor"]) == "Author:2"


def test_connection_keyset_pagination_by_pk(graphql_client):
    """first/after keyset pagination over the default PK ordering."""
    page1 = graphql_client(
        "{ authors(first: 1, order: [{ field: { id: ASC } }]) "
        "  { totalCount edges { node { name } } pageInfo { hasNextPage endCursor } } }"
    )
    conn = page1["data"]["authors"]
    assert conn["totalCount"] == 2
    assert [n["name"] for n in _edge_nodes(conn)] == ["Alice"]
    assert conn["pageInfo"]["hasNextPage"] is True
    cursor = conn["pageInfo"]["endCursor"]
    page2 = graphql_client(
        f'{{ authors(first: 1, after: "{cursor}", order: [{{ field: {{ id: ASC }} }}]) '
        "  { edges { node { name } } pageInfo { hasNextPage } } }"
    )
    assert "errors" not in page2, page2
    assert [n["name"] for n in _edge_nodes(page2["data"]["authors"])] == ["Bob"]
    assert page2["data"]["authors"]["pageInfo"]["hasNextPage"] is False


def test_connection_keyset_pagination_honours_order(graphql_client):
    """Keyset cursors honour a non-PK order; cursor encodes the sort key + PK."""
    page1 = graphql_client(
        "{ books(first: 1, order: [{ field: { title: ASC } }]) "
        "  { edges { cursor node { title } } pageInfo { endCursor } } }",
        token="alice-token",
    )
    edges = page1["data"]["books"]["edges"]
    assert [n["title"] for n in (e["node"] for e in edges)] == ["Public Alice"]
    # cursor key = title|id  ->  'Book:Public%20Alice|1'
    assert _decode_cursor(edges[0]["cursor"]).startswith("Book:Public%20Alice|")
    cursor = page1["data"]["books"]["pageInfo"]["endCursor"]
    page2 = graphql_client(
        f'{{ books(first: 1, after: "{cursor}", order: [{{ field: {{ title: ASC }} }}]) '
        "  { edges { node { title } } } }",
        token="alice-token",
    )
    assert [e["node"]["title"] for e in page2["data"]["books"]["edges"]] == ["Public Bob"]


def test_connection_limit_offset(graphql_client):
    """limit/offset pagination honours order and reports hasPreviousPage."""
    body = graphql_client(
        "{ authors(limit: 1, offset: 1, order: [{ field: { id: ASC } }]) "
        "  { nodes { name } pageInfo { hasPreviousPage hasNextPage } } }"
    )
    assert "errors" not in body, body
    conn = body["data"]["authors"]
    assert [n["name"] for n in conn["nodes"]] == ["Bob"]
    assert conn["pageInfo"]["hasPreviousPage"] is True
    assert conn["pageInfo"]["hasNextPage"] is False


def test_connection_composite_pk(graphql_client):
    """Composite-PK table works; cursor encodes both PK columns joined by '|'."""
    body = graphql_client(
        "{ bookTags(order: [{ field: { bookId: ASC } }, { field: { tag: ASC } }]) "
        "  { totalCount edges { cursor node { bookId tag } } } }"
    )
    assert "errors" not in body, body
    conn = body["data"]["bookTags"]
    assert conn["totalCount"] == 3
    first = conn["edges"][0]
    assert (first["node"]["bookId"], first["node"]["tag"]) == (1, "classic")
    assert _decode_cursor(first["cursor"]) == "BookTag:1|classic"


def test_connection_nodes_shape_eager_loads_to_one(graphql_client):
    """Nested to-one under the flat `nodes` shape eager-loads (no lazy-load error)."""
    body = graphql_client(
        "{ books(order: [{ field: { id: ASC } }]) { nodes { title author { name } } } }", token="alice-token"
    )
    assert "errors" not in body, body
    assert body["data"]["books"]["nodes"][0]["author"]["name"] == "Alice"


def test_graphql_pk_lookup(graphql_client):
    """Primary-key lookup returns a single record (raw int PK arg)."""
    body = graphql_client("{ author(id: 1) { name } }")
    assert "errors" not in body, body
    assert body["data"]["author"]["name"] == "Alice"


def test_graphql_nested_relationship(graphql_client):
    """Authenticated nested query traverses author -> books via the optimizer.

    The to-many relation field is ``books`` and the to-one is ``author`` (WS2:
    relationship names are singularized/pluralized at the automap layer).
    """
    body = graphql_client(
        "{ authors(order: [{ field: { id: ASC } }]) { edges { node { name books { title } } } } }",
        token="alice-token",
    )
    assert "errors" not in body, body
    authors = {n["name"]: {b["title"] for b in n["books"]} for n in _edge_nodes(body["data"]["authors"])}
    # app_author sees all of Alice's books (public + private).
    assert {"Public Alice", "Secret Alice"} <= authors["Alice"]


def test_filter_object_traversal_cyclic_limitation(graphql_client):
    """Pin the strawberry-orm 0.13.0 cyclic-relation `object`-filter limitation.

    ``orm.filter()`` wires a relation into its ``object`` filter only if the
    related model's filter is already registered. SQLAlchemy automap creates
    relationships in *both* directions for every FK (``authors.books``
    and ``books.authors``), forming a 2-cycle, so exactly one direction gets an
    ``object`` key depending on registration order. Here ``booksFilter`` exposes
    ``object`` (filter books by their author) but ``authorsFilter`` does not
    (cannot filter authors by their books). See the spec's friction log.

    This test locks in the current behaviour; if a future strawberry-orm release
    wires both directions, it will fail and prompt a docs/update.
    """
    body = graphql_client(
        '{ b: __type(name: "booksFilter") { inputFields { name } }'
        '  a: __type(name: "authorsFilter") { inputFields { name } } }'
    )
    assert "errors" not in body, body
    books_fields = {f["name"] for f in body["data"]["b"]["inputFields"]}
    authors_fields = {f["name"] for f in body["data"]["a"]["inputFields"]}
    assert "object" in books_fields, "expected books->author object traversal"
    assert "object" not in authors_fields, "authors->books object traversal unexpectedly wired"

    # The wired direction works functionally: filter books by related author.
    body = graphql_client(
        "{ books("
        '    filter: { object: { author: { field: { name: { exact: "Alice" } } } } }'
        "    order: [{ field: { id: ASC } }]"
        "  ) { edges { node { title } } } }",
        token="alice-token",
    )
    assert "errors" not in body, body
    assert {n["title"] for n in _edge_nodes(body["data"]["books"])} == {"Public Alice", "Secret Alice"}


def test_function_set_returning(graphql_client):
    """WS5: a STABLE function returning SETOF a table maps to a list of nodes."""
    body = graphql_client("{ publicBooks { title } }", token="alice-token")
    assert "errors" not in body, body
    titles = {r["title"] for r in body["data"]["publicBooks"]}
    assert titles == {"Public Alice", "Public Bob"}


def test_function_scalar_under_rls(graphql_client):
    """WS5: a SCALAR function runs under the request role (RLS applies)."""
    anon = graphql_client("{ bookCount }")
    assert "errors" not in anon, anon
    assert anon["data"]["bookCount"] == 2  # anon sees only public books
    authed = graphql_client("{ bookCount }", token="alice-token")
    assert "errors" not in authed, authed
    assert authed["data"]["bookCount"] == 3  # app_author sees private too


def test_graphql_column_description(graphql_client):
    """WS3: a column's smart comment becomes the GraphQL field description."""
    body = graphql_client('{ __type(name: "Book") { fields { name description } } }')
    assert "errors" not in body, body
    descriptions = {f["name"]: f["description"] for f in body["data"]["__type"]["fields"]}
    assert descriptions["title"] == "The book title."


def test_graphql_jsonb_column(graphql_client):
    """A JSONB column is exposed via the JSON scalar (not a bare dict)."""
    body = graphql_client(
        '{ books(filter: { field: { title: { exact: "Public Alice" } } })   { edges { node { title attributes } } } }',
        token="alice-token",
    )
    assert "errors" not in body, body
    assert _edge_nodes(body["data"]["books"])[0]["attributes"] == {"genre": "fiction"}


def test_graphql_to_one_relationship(graphql_client):
    """Traverse the to-one direction book -> author (WS2: singular ``author``)."""
    body = graphql_client(
        "{ books(order: [{ field: { id: ASC } }]) { edges { node { title author { name } } } } }",
        token="alice-token",
    )
    assert "errors" not in body, body
    first = _edge_nodes(body["data"]["books"])[0]
    assert first["author"]["name"] == "Alice"


def test_graphql_native_filter(graphql_client):
    """Native @oneOf filter narrows the result set."""
    body = graphql_client(
        '{ authors(filter: { field: { name: { exact: "Bob" } } }) { edges { node { name } } } }',
    )
    assert "errors" not in body, body
    names = [n["name"] for n in _edge_nodes(body["data"]["authors"])]
    assert names == ["Bob"]


def test_rls_anonymous_sees_only_public_books(graphql_client):
    """RLS: an anonymous request (app_anon role) sees only public books."""
    body = graphql_client("{ books { edges { node { title visibility } } } }")
    assert "errors" not in body, body
    titles = {n["title"] for n in _edge_nodes(body["data"]["books"])}
    assert titles == {"Public Alice", "Public Bob"}
    assert "Secret Alice" not in titles


def test_rls_authenticated_sees_all_books(graphql_client):
    """RLS: an authenticated request (app_author role) sees private books too."""
    body = graphql_client("{ books { edges { node { title } } } }", token="alice-token")
    assert "errors" not in body, body
    titles = {n["title"] for n in _edge_nodes(body["data"]["books"])}
    assert "Secret Alice" in titles


def test_rls_nested_anonymous_does_not_leak(graphql_client):
    """RLS holds through a nested relation load, not just the root query.

    The strawberry-orm docs warn that parent scoping does not flow to children;
    here the role-scoped session must make the nested books load honour RLS too.
    """
    body = graphql_client("{ authors { edges { node { name books { title } } } } }")
    assert "errors" not in body, body
    by_author = {n["name"]: {b["title"] for b in n["books"]} for n in _edge_nodes(body["data"]["authors"])}
    # Anonymous: Alice's nested books exclude the private one.
    assert "Secret Alice" not in by_author.get("Alice", set())
    assert "Public Alice" in by_author.get("Alice", set())


def test_mutation_create_and_delete(graphql_client):
    """create<Singular> then delete<Singular> by primary key (RETURNING-based)."""
    body = graphql_client('mutation { createAuthor(input: { name: "Carol" }) { id name } }', token="alice-token")
    assert "errors" not in body, body
    created = body["data"]["createAuthor"]
    assert created["name"] == "Carol"
    raw = created["id"]  # native raw integer PK (no relay GlobalID)

    body = graphql_client(f"mutation {{ deleteAuthor(id: {raw}) {{ name }} }}", token="alice-token")
    assert "errors" not in body, body
    assert body["data"]["deleteAuthor"]["name"] == "Carol"


def test_mutation_create_many_and_delete_many(graphql_client):
    """create<Plural> inserts many; delete<Plural> removes them via a where filter."""
    body = graphql_client(
        'mutation { createAuthors(inputs: [{ name: "D1" }, { name: "D2" }]) { name } }',
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
    raw = created["data"]["createAuthor"]["id"]
    try:
        body = graphql_client(
            f'mutation {{ updateAuthor(id: {raw}, patch: {{ name: "Renamed" }}) {{ name }} }}',
            token="alice-token",
        )
        assert "errors" not in body, body
        assert body["data"]["updateAuthor"]["name"] == "Renamed"
    finally:
        graphql_client(f"mutation {{ deleteAuthor(id: {raw}) {{ name }} }}", token="alice-token")


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


async def test_ws1_role_reapplied_on_each_transaction(configured_app):
    """WS1: the ``after_begin`` hook re-applies the role to post-commit transactions.

    Directly exercises the mechanism: set ``app_writer``, read the role,
    ``commit`` (ending the transaction-local config), then read again in the new
    transaction. Without re-application the second read sees ``none`` (the GUC
    default); with it, ``app_writer`` persists.

    Builds its own engine/session in the running event loop (the module engine
    belongs to a different loop, which asyncpg rejects).
    """
    import uuid

    from sqlalchemy import event, text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from fusionserve import auth, graphql, persistence

    s = configured_app
    url = f"postgresql+asyncpg://{s.pg_user}:{s.pg_password.get_secret_value()}@{s.pg_host}:{s.pg_port}/{s.pg_database}"
    engine = create_async_engine(url)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    user = auth.User(id=uuid.UUID("00000000-0000-0000-0000-000000000009"), username="w", role="app_writer")
    session = maker()
    session.info["fs_user"] = user
    event.listen(session.sync_session, "after_begin", graphql._reapply_role_on_begin)
    try:
        await persistence.set_role(session, user)
        first = (await session.execute(text("SELECT current_setting('role', true)"))).scalar()
        assert first == "app_writer"
        await session.commit()
        second = (await session.execute(text("SELECT current_setting('role', true)"))).scalar()
        assert second == "app_writer", f"role not re-applied post-commit: {second!r}"
    finally:
        await session.close()
        await engine.dispose()


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
        body = graphql_client("{ authors { edges { node { name books { title } } } } }", token="alice-token")
    finally:
        event.remove(sync_engine, "after_cursor_execute", _count)
    assert "errors" not in body, body
    # 2 seeded authors; a per-row (N+1) load would scale with author count.
    # Expect ~3 SELECTs (totalCount, authors, then a single batched books load)
    # plus minor overhead — bounded well under a per-author count.
    assert len(selects) <= 6, f"unexpected SELECT count {len(selects)}: {selects}"
