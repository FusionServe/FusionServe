# FusionServe

> Automatic generation of REST and GraphQL APIs by database introspection.

FusionServe introspects a PostgreSQL database schema and automatically
generates both REST and GraphQL APIs, making it easy to expose database
tables as web APIs without manually writing endpoints.

## Features

- **Automatic API Generation** — introspects your PostgreSQL schema and
  creates full CRUD endpoints.
- **Dual API Support** — REST API with OpenAPI docs + GraphQL API.
- **OData-style Filtering** — advanced query filtering using OData syntax.
- **Pagination** — built-in pagination with configurable page size.
- **Role-based Security** — PostgreSQL role-based access control with
  per-request `SET ROLE` and row-level security.
- **JWT authentication** — JWKS-based RS256 verification with OIDC
  discovery support.
- **Prometheus Metrics** — built-in `/metrics` endpoint.
- **Brotli/GZip Compression** — optimized response compression.

## Quick Start

```bash
# Install
uv sync --all-groups

# Run with uvicorn
uv run uvicorn fusionserve.main:app --reload --port 8001
```

> The app is built on **Litestar**. The `[project.scripts]` entry has been
> removed; do not run `uv run fusionserve` — invoke `uvicorn` (or `granian`
> for production) against the ASGI app directly.

Or using Docker:

```bash
docker build -t fusionserve .
docker run --env-file .env -p 8001:8001 fusionserve
```

## Configuration

FusionServe uses [Pydantic Settings](https://github.com/pydantic/pydantic-settings)
for configuration. It loads variables from a local `.env` file and from
process environment variables; environment variables win.

### `.env` file

A working template ships as [`.env.example`](.env.example). Copy it to
`.env` and fill in real values for local development. **Never commit a
real `.env` — the repository's `.gitignore` excludes it.**

### Configuration options

| Setting | Default | Description |
|---|---|---|
| `app_name` | `FusionServe` | Application name (also the logger name). |
| `log_level` | `INFO` | Logging level. |
| `debug` | `False` | Enable Litestar debug mode. |
| `base_path` | `/api` | URL prefix for REST controllers and GraphQL. |
| `pg_host` | `localhost` | PostgreSQL host. |
| `pg_port` | `5432` | PostgreSQL port. |
| `pg_user` | `fusionserve` | PostgreSQL user used for introspection / async queries. |
| `pg_password` | _(empty)_ | PostgreSQL password. |
| `pg_database` | `fusionserve` | PostgreSQL database name. |
| `pg_app_schema` | `app_public` | Schema to introspect. |
| `echo_sql` | `False` | Log SQL queries via SQLAlchemy `echo`. |
| `max_page_size` | `1000` | Hard upper bound on a page size. |
| `anonymous_role` | `fusionserve` | PostgreSQL role assumed for unauthenticated requests. |
| `jwt_issuer` | _(unset)_ | OIDC issuer URL; used for `iss` validation and JWKS discovery. |
| `jwks_url` | _(unset)_ | Optional explicit JWKS endpoint (skips OIDC discovery). |
| `client_id` | `app_name.lower()` | OAuth2 client id used to locate roles in the access token. |

### Required PostgreSQL privileges

On startup, `persistence.introspect()` issues a
`CREATE OR REPLACE FUNCTION <pg_app_schema>.current_user_id()` statement
so the configured RLS policies can resolve the authenticated user id from
the per-request `user.id` setting. The role used for introspection
(`pg_user`) therefore needs:

- `CREATE` and `USAGE` privileges on the configured schema
  (`pg_app_schema`).
- The ability to create or replace functions in that schema (typically
  schema ownership or membership of the schema owner role).
- `SELECT` access to system catalogues for SQLAlchemy reflection.

If you would rather manage the function out of band (e.g. through a
migration tool), drop privileges accordingly and remove the DDL block
from `persistence.introspect`.

## REST API

Once running, the Scalar / Swagger documentation is at `/api/docs` and
the OpenAPI document at `/api/openapi.json`.

### Endpoints

For each table (e.g. `users`):

| Method | Endpoint | Description |
|---|---|---|
| GET | `/api/users` | List records (filter / paginate). |
| GET | `/api/users/{pk}` | Get one record by primary key. |
| POST | `/api/users` | Create a record. |
| PATCH | `/api/users/{pk}` | Update a record. |
| DELETE | `/api/users/{pk}` | Delete a record. |

### Query parameters

**Pagination:**

```text
GET /api/users?_limit=10&_offset=0
```

**Basic equality filtering:**

```text
GET /api/users?status=active&role=admin
```

**Advanced OData filtering:**

```text
GET /api/users?_filter=(status eq 'active') and (age gt 18)
```

Supported OData operators: `eq`, `ne`, `gt`, `ge`, `lt`, `le`, `and`,
`or`, `not`.

## GraphQL API

`POST /api/graphql` exposes the schema. The GraphiQL IDE is mounted at
the same path when accessed via a browser.

### Query example

```graphql
query {
  users(limit: 10, offset: 0) {
    nodes {
      id
      name
      email
    }
    totalCount
  }
}
```

## Architecture

```text
src/fusionserve/
├── main.py        # Litestar application entry point
├── config.py      # Pydantic-settings configuration
├── persistence.py # Database introspection & engine setup
├── rest.py        # REST API route generation
├── graphql.py     # GraphQL schema generation
├── auth.py        # JWT verification and User model
└── models.py      # Pydantic / Strawberry helper models
```

### How it works

1. **Startup** — Litestar's lifespan callback runs
   `persistence.introspect()`, reflecting the configured schema using a
   synchronous psycopg engine, and registers the dynamically built REST
   controllers and GraphQL schema on the app instance.
2. **Model generation** — Pydantic and Strawberry types are derived from
   each ORM class's `__table__` at controller / schema build time. There
   is no codegen step.
3. **Request handling** — every resolver opens a fresh async session
   and calls `persistence.set_role(session, user)` so RLS policies see
   the right `role` and `user.*` settings.

### Built-in endpoints

| Path | Description |
|---|---|
| `/api/docs` | OpenAPI documentation (Swagger + Scalar). |
| `/api/openapi.json` | OpenAPI specification. |
| `/api/graphql` | GraphQL endpoint (with GraphiQL IDE). |
| `/metrics` | Prometheus metrics. |
