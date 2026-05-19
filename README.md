# FusionServe

> Automatic generation of REST and GraphQL APIs by database introspection.

FusionServe introspects a PostgreSQL database schema and automatically
generates both REST and GraphQL APIs, making it easy to expose database
tables as web APIs without manually writing endpoints.

## Features

- **Automatic API Generation** — introspects your PostgreSQL schema and
  creates full CRUD endpoints.
- **Dual API Support** — REST API + GraphQL API.
- **Built-in admin UI** — modern React SPA (Vite + TanStack Router/Query
  + Tailwind v4) mounted at `/api/`, embedding Scalar for OpenAPI
  exploration and a GraphiQL link.
- **OData-style Filtering** — advanced query filtering using OData syntax.
- **Pagination** — built-in pagination with configurable page size.
- **Role-based Security** — PostgreSQL role-based access control with
  per-request `SET ROLE` and row-level security.
- **JWT authentication** — JWKS-based RS256 verification with OIDC
  discovery support.
- **File uploads** — multi-file upload over REST with a pluggable
  storage backend (local filesystem, S3, or a custom dotted-import
  class). See `docs/features/file_uploads.md`.
- **Prometheus Metrics** — built-in `/metrics` endpoint.
- **Brotli/GZip Compression** — optimized response compression.

## Quick Start

```bash
# Install Python dependencies
uv sync --all-groups

# Build the bundled UI (requires pnpm: https://pnpm.io; corepack will
# auto-activate the pinned version from ``ui/package.json``)
cd ui && pnpm install && pnpm run build && cd ..

# Run with uvicorn
uv run uvicorn fusionserve.main:app --reload --port 8001
```

> The app is built on **Litestar**. The `[project.scripts]` entry has been
> removed; do not run `uv run fusionserve` — invoke `uvicorn` (or `granian`
> for production) against the ASGI app directly.

> The bundled React UI ships inside the Python wheel at
> `src/fusionserve/web/dist`. Litestar serves it via a static-files
> router at `settings.ui_path` (default `/api/-/`); if the directory
> is missing or `index.html` is absent, requests to `<ui_path>` 404
> at request time. For frontend development run `pnpm run dev` in
> `ui/` — the Vite dev server starts at `http://localhost:5173/` and
> proxies `/api/*` to the backend on `:8001`.

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
| `ui_enabled` | `True` | When `False`, disables the integrated UI and the OpenAPI render plugins. |
| `ui_path` | `f"{base_path}/-/"` (i.e. `/api/-/`) | Public URL where the React SPA is mounted. `/api/` issues a 302 redirect here. Derives from `base_path` when unset; override with `UI_PATH=…`. |

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

## Built-in UI

The bundled React SPA is served by a single Litestar static-files
router at `settings.ui_path` (default `/api/-/`, derived from
`base_path`), wired up in `fusionserve.ui`. Users typically arrive
via a 302 redirect from `/api/` to `settings.ui_path` issued by
`fusionserve.ui.RedirectRenderPlugin`. Vite emits *relative* asset
URLs (`./assets/<hash>.<ext>` in `index.html`), so the same router
serves the hashed JS/CSS chunks at `<ui_path>/assets/...` without a
second mount — the SPA is location-independent and can be relocated
by changing only `ui_path`. The SPA iframes the backend's Swagger UI
(`/api/swagger`) and GraphiQL IDE (`/api/graphql`) so the bundle
stays small; the raw OpenAPI 3.1 document at `/api/openapi.json` and
the Scalar viewer at `/api/scalar` remain available for direct
access. Client-side navigation uses hash routing
(`/api/-/#/openapi`, `/api/-/#/graphql`, …) so the SPA router stays
immune to any future top-level Litestar route.

For frontend development run `pnpm run dev` in `ui/`: the Vite dev
server starts at `http://localhost:5173/` and proxies `/api/*`
requests (REST, GraphQL, OpenAPI surfaces) to the Litestar backend on
`:8001`. In dev the SPA is reached at the dev-server root (Vite's
history fallback serves `index.html`), not at `<ui_path>`; hash
routing keeps deep links identical between dev and prod apart from
the prefix.

## REST API

The OpenAPI document is served at `/api/openapi.json`. Two interactive
viewers are also available out of the box: Swagger UI at `/api/swagger`
and Scalar at `/api/scalar` (both backed by the same auto-generated
schema). The React SPA at `settings.ui_path` iframes Swagger UI; the
top-level `/api/` URL redirects there.

### Endpoints

REST endpoints are versioned under `/api/v1/` to keep the URL surface
forward-compatible. For each table (e.g. `users`):

| Method | Endpoint | Description |
|---|---|---|
| GET | `/api/v1/users` | List records (filter / paginate). |
| GET | `/api/v1/users/{pk}` | Get one record by primary key. |
| POST | `/api/v1/users` | Create a record. |
| PATCH | `/api/v1/users/{pk}` | Update a record. |
| DELETE | `/api/v1/users/{pk}` | Delete a record. |

### Query parameters

**Pagination:**

```text
GET /api/v1/users?_limit=10&_offset=0
```

**Basic equality filtering:**

```text
GET /api/v1/users?status=active&role=admin
```

**Advanced OData filtering:**

```text
GET /api/v1/users?_filter=(status eq 'active') and (age gt 18)
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
├── rest.py        # REST API route generation (/api/v1/<table>)
├── graphql.py     # GraphQL schema generation (/api/graphql)
├── ui.py          # React SPA wiring + RedirectRenderPlugin
├── auth.py        # JWT verification and User model
└── models.py      # Pydantic / Strawberry helper models

ui/                # pnpm + Vite + React + TS + Tailwind v4 SPA
└── src/           # Built into ../src/fusionserve/web/dist
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
| `/api/` | 302 redirect to the React UI at `settings.ui_path` (default `/api/-/`). |
| `/api/openapi.json` | OpenAPI specification (raw JSON). |
| `/api/swagger` | Swagger UI. |
| `/api/scalar` | Scalar API reference. |
| `/api/v1/<table>` | REST CRUD endpoints generated from PostgreSQL introspection. |
| `/api/graphql` | GraphQL endpoint (with GraphiQL IDE on `GET`). |
| `<ui_path>` (default `/api/-/`, and any `<ui_path>/{path}`) | React SPA + hashed Vite assets, served by a single Litestar static-files router with `html_mode=True`. Configurable via `settings.ui_path`. |
| `/metrics` | Prometheus metrics. |
