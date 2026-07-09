# AGENTS.md

Compact guide for agents working on FusionServe. For broader context see
`README.md` and `DEVELOPMENT.md` — this file only captures what those miss
or get wrong.

## Toolchain

- Python **3.14+** is required (`requires-python = ">=3.14"` in `pyproject.toml`).
- [`uv`](https://docs.astral.sh/uv/) is the package manager and runner.
  Do **not** use `pip` / `poetry` / `venv` directly.
- Bootstrap: `uv sync --all-groups` (dev group is needed for ruff, pytest,
  pre-commit, mkdocs tooling).
- The bundled SPA under `ui/` uses **pnpm** exclusively. Do **not**
  run `npm` / `npx` / `bun` / `bunx` / `yarn` inside `ui/`. The pnpm
  version is pinned via the `packageManager` field in
  `ui/package.json` and resolved by corepack (`corepack enable` then
  any `pnpm …` invocation downloads the pinned version on first use).
  Install with `pnpm install`, run scripts with `pnpm run …`, and
  commit `pnpm-lock.yaml` alongside any `package.json` change.

## Commands CI enforces (run in this order)

CI (`.github/workflows/ci.yml`) fails on any of these — mirror it locally
before pushing:

```bash
uv run ruff check .          # lint (no --fix in CI)
uv run ruff format --check . # format check (no rewrites in CI)
uv run pytest -q             # tests
```

For local iteration use `uv run ruff check --fix .` and `uv run ruff format .`.

## Running the app

**Don't use `uv run fusionserve`** — the `[project.scripts]` entry points at
`fusionserve.main:run`, which does not exist. It will `ImportError`.
Use the ASGI app object directly:

```bash
uv run uvicorn fusionserve.main:app --reload --port 8001
```

The app needs a reachable PostgreSQL on startup — introspection happens in
the Litestar lifespan (`main.lifespan` → `persistence.introspect`), so the
process will not come up without the database.

## Config & secrets

- Config is `pydantic-settings` (`src/fusionserve/config.py`), loads `.env`
  and uppercased env vars. Default `pg_app_schema` is `app_public` (the
  README's "public" claim is wrong).
- `.env` is **committed** and currently contains a real-looking password.
  Never add new secrets to `.env`; override via environment variables in
  deployment. Flag any change to `.env` in review.

## Architecture that is not obvious

- Web framework is **Litestar**, not FastAPI (the README mentions FastAPI
  in one place; ignore it — verify against `main.py`).
- There is no static route definition and no codegen step. Every REST
  controller and every GraphQL field is built **at runtime** during the
  Litestar lifespan from live PG introspection:
  - `persistence.introspect()` reflects the schema via a **sync** psycopg
    engine (SQLAlchemy reflection requires sync), then hands the automap
    `Base` to `rest.build` and `graphql.build` — both functions iterate
    `Base.classes` and derive every type they need from each ORM class's
    `__table__`; there is no shared registry. Runtime queries use the async
    asyncpg engine. Both dialects must work.
  - `introspect()` **rejects any table whose name is not plural** (checked
    with `inflect.singular_noun`). Adding a singularly-named table will
    crash startup with `ValueError: Table name X is not plural`.
  - `introspect()` also issues a `CREATE OR REPLACE FUNCTION
    <pg_app_schema>.current_user_id()` DDL on every startup — the DB role
    used for introspection must have privileges to do so.
- Per-request PG role switching: every REST/GraphQL resolver opens its own
  `async_session()` and calls `persistence.set_role(session, user)` which
  issues `set_config('role', ...)` plus `user.*` settings. Unauthenticated
  requests fall back to `settings.anonymous_role`. Any new resolver that
  opens a session must call `set_role` before executing queries, otherwise
  row-level security will silently use the wrong role.
- GraphQL schema construction (`graphql.build`) dynamically attaches fields
  to the module-level `Query` / `Mutation` classes. Resolver signatures are
  rewritten post-hoc by reassigning `base_resolver.arguments` with
  `StrawberryArgument` instances — follow the existing pattern when adding
  new resolvers, don't try to declare arguments with plain Python annotations
  for dynamically-generated input types.
- GraphQL CRUD is RETURNING-based: `update_resolver`, `update_many_resolver`,
  `delete_resolver`, `delete_many_resolver` rely on PostgreSQL
  `... RETURNING *` for single-roundtrip mutations. Keep this when touching
  those resolvers; don't reintroduce SELECT-then-mutate patterns.
- `update_many` / `delete_many` intentionally raise `ValueError` when the
  resolved `where` condition is `None` (empty filter), to block accidental
  table-wide writes. Don't "fix" this by defaulting to no-op.
- The bundled React SPA is served by **two** handlers, wired in
  `src/fusionserve/ui.py` (`build_spa_route_handler`, which returns a
  sequence — `main.py` does `route_handlers.extend(...)`): (1) an assets
  static-files router at `<ui_path>assets` serving `dist/assets`
  (`html_mode=False`), and (2) a base-href-injecting `index.html` handler
  registered for `<ui_path>` and `<ui_path>{path:path}` (the deep-link
  fallback under browser-history routing). Both carry
  `opt={"exclude_from_auth": True}` so the auth middleware skips them via
  its `exclude_opt_key` mechanism — do not add the URL patterns to
  `auth_mw.exclude`.
- There is no separate asset URL setting. Vite builds with `base: "./"`
  (see `ui/vite.config.ts`) so `index.html` references chunks via relative
  URLs (`./assets/...`). Because the SPA uses **path routing**, those
  resolve against the document's `<base href>` — `index.html` ships
  `<base href="/">` and `_render_index()` rewrites it to `Settings.ui_path`
  before serving, so chunks resolve to `<ui_path>/assets/<hash>.<ext>` for
  any route. The SPA reads its router basepath from `document.baseURI`
  (`ui/src/lib/router.ts`). This keeps the SPA location-independent —
  relocating it is a one-setting change (`UI_PATH=…`), no JS rebuild.
- Users land on the SPA via a 302 redirect from `settings.base_path`
  (`/api/`) issued by `fusionserve.ui.RedirectRenderPlugin`. That
  plugin is registered as the first entry of
  `OpenAPIConfig.render_plugins`, becoming the default plugin; the
  upstream OpenAPI router auto-mounts it at its router root. The plugin
  returns a `litestar.response.Redirect` from `render()` despite the
  upstream `-> bytes` annotation — Litestar honours `Response` returns
  regardless of the declared type.
- `Settings.ui_path` derives from `Settings.base_path` via the
  `_derive_ui_path` `model_validator(mode="after")` in
  `fusionserve.config`: the default is sentinel `""`, which the
  validator fills with `f"{base_path.rstrip('/')}/-/"` (i.e. `/api/-/`
  for the default `base_path`). Setting `UI_PATH=…` explicitly skips
  the branch. The trailing-slash form is canonical — `<base_path>/-`
  (no slash, single segment) is matched by the OpenAPI router's
  `<base_path>/{path:str}` not-found handler and 404s; the
  `<base_path>/` -> `<ui_path>` redirect emits the slash so users on
  the normal entry path see the SPA.
- The static-files router does **not** require `index.html` to exist
  at startup — it 404s at request time if the file is missing. Run
  `pnpm run build` in `ui/` to populate `src/fusionserve/web/dist`
  before serving the SPA in production. Frontend development uses a
  standalone Vite dev server: `pnpm run dev` in `ui/` starts it at
  `http://localhost:5173/` with `server.proxy` forwarding `/api/*`
  requests to the Litestar backend on `:8001` (target hard-coded in
  `ui/vite.config.ts`). Guardrail: `tests/test_ui.py` pins the
  assets-router-at-`<ui_path>assets` + index-handler (with `{path:path}`
  fallback and base-href injection) + `exclude_from_auth=True` shape.
- REST endpoints are versioned under `<base_path>/v1/<table>` (default
  `/api/v1/<table>`); the same `v1` prefix applies to PG-function
  controllers. The version segment is hard-coded in `fusionserve.rest`;
  bump via a single grep when introducing a `/v2`.
- File uploads are an **opt-in** feature gated on the presence of an
  operator-supplied `uploads` table (name configurable via
  `STORAGE_METADATA_TABLE`). The specialized controller in
  `fusionserve.files` mounts at `<base_path>/v1/_uploads` (leading
  underscore namespaces it away from the auto-generated CRUD at
  `<base_path>/v1/uploads`). GraphQL is deliberately untouched — do
  not introduce schema-build branches keyed on the metadata table
  name; lock writes down via RLS on the operator side.
  - Uploads are **direct-to-store and two-phase**: `POST /_uploads`
    (init) inserts a `pending` row per file and returns a presigned
    upload URL (`StorageBackend.generate_upload_url`); the client PUTs
    bytes straight to the store; `POST /_uploads/{id}/complete` HEADs
    the object (`stat`), enforces `STORAGE_MAX_SINGLE_FILE_BYTES`
    (deleting + 413 on breach), and flips the row to `completed` with
    the verified size/etag. Bytes never pass through the app in this
    path — do **not** reintroduce a `save`/`open` streaming Protocol.
  - Download (`GET /_uploads/{id}/content`) always 302-redirects to a
    presigned GET URL. The cascading delete at `DELETE /_uploads/{id}`
    removes the blob then the row; the auto-generated
    `DELETE /api/v1/uploads/{id}` only removes the row and orphans the
    blob.
  - The metadata table contract (validated at startup by
    `files.metadata.validate_uploads_table`) requires `status` and
    allows `size_bytes`/`etag`/`attributes` to be NULL. The JSONB bag
    is named `attributes` **not** `metadata` — a column named
    `metadata` collides with SQLAlchemy's reserved Declarative
    attribute and breaks automap introspection at startup.
  - Optional HTTP proxy (`STORAGE_PROXY_URLS=true`,
    `fusionserve.files.proxy`): the presigned upload/download URLs are
    origin-swapped to `<base_path>/v1/_uploads/_proxy/...` (path+query
    preserved so the signature stays valid); the `_proxy` relay
    handlers reconstruct the target from `StorageBackend.object_origin()`
    (never from client input — anti-SSRF) and stream via `httpx`. The
    relay routes carry `opt={"exclude_from_auth": True}` and set a
    per-handler `request_max_body_size` for uploads — the signed URL is
    the capability, like a raw presigned URL.
  - Storage backend selection (`STORAGE_BACKEND`, default `"s3"`)
    accepts `"s3"`, `"azure"` (an unimplemented placeholder whose
    methods raise `NotImplementedError`), or a `"pkg.mod:Class"` dotted
    import path resolved by `fusionserve.storage.load_backend` — custom
    backends must implement the `StorageBackend` Protocol in
    `fusionserve.storage.base` (`generate_upload_url`,
    `generate_download_url`, `stat`, `delete`, `object_origin`) and be
    instantiable with no arguments (read your own settings from
    `fusionserve.config`). There is no import-time bucket validation:
    `S3Backend.__init__` raises if `STORAGE_S3__BUCKET` is unset, which
    surfaces at lifespan startup only when the feature is active.
- The SPA uses **hash routing** (`createHashHistory`) so client-side
  paths (`/-/#/openapi`, `/-/#/graphql`, …) cannot collide with any
  future top-level Litestar route. Browser history would also work
  given the current layout, but hash routing is one fewer thing to
  reason about.
- The SPA uses **browser-history (path) routing** (`createBrowserHistory`,
  `ui/src/lib/router.ts`). Client-side deep links (`<ui_path>data`,
  `<ui_path>graphql`, …) reload via the index handler's `{path:path}`
  fallback (dev: Vite's history fallback). The router `basepath` is read
  from `document.baseURI` (driven by the injected `<base href>`), so no
  build-time mount constant is needed. Multi-segment SPA paths don't
  collide with the OpenAPI router's single-segment `<base_path>/{path:str}`
  not-found handler.
- The canonical OpenAPI document lives at `/api/openapi.json`,
  auto-registered by Litestar's upstream OpenAPI router: none of the
  configured `render_plugins` (Redirect / Swagger / Scalar) claims that
  path, so the upstream "if no plugin registered openapi.json, add the
  JsonRenderPlugin fallback" branch in `litestar/_openapi/plugin.py`
  fires. If you ever want to override the media type or behaviour,
  prepend an explicit `JsonRenderPlugin` to `render_plugins` in
  `main.py`.
- There is no `/api/_meta` endpoint. The SPA used to fetch a runtime
  introspection catalogue from there; that machinery was removed when
  the UI scope shrank to "redirect → SPA shell that iframes the
  backend-served Swagger UI (`/api/swagger`) and GraphiQL
  (`/api/graphql`) viewers". Future data-fetching features should
  reintroduce a dedicated endpoint (avoid resurrecting the deleted
  `MetaResponse` shape verbatim).

## Style & conventions

- Ruff is configured with `line-length = 120` and rules `E, F, UP, B, SIM, I`
  (isort is part of ruff — do not run a separate isort).
- Public functions use **Google-style** docstrings. `mkdocs` + `mkdocstrings`
  generates the API reference from them, and members whose names start with
  `_` are filtered out of the generated docs (see `filters: ["!^_"]` in
  `mkdocs.yaml`).
- `pre-commit` includes the `uv-lock` hook — whenever you change
  `pyproject.toml`, run `uv sync` and commit `uv.lock` in the **same commit**
  or pre-commit will block the push. CI uses `uv sync --frozen`; a drifted
  lockfile breaks CI.

## Tests

- `tests/` currently has unit coverage for `auth`, `persistence`,
  `graphql_helpers`, and `ui` (the new `/api/_meta` projector), plus a
  smoke test (`test_skeleton.py`) that just checks `__version__`. Real
  integration tests live in `test_integration_introspection.py` behind the
  `integration` mark and are gated on `RUN_INTEGRATION=1` (CI keeps the
  job disabled with `if: false`). When adding features, mirror the
  existing pattern: prefer unit tests against pure helpers and mock the
  introspection boundary.
- `pytest` runs without a database because nothing in the unit suite
  imports `main` (which triggers introspection). If you add tests that
  touch `main`, `rest.build`, or `graphql.build`, you will need to stand
  up a PG instance via `testcontainers` (already a dev dependency) or
  mock the introspection boundary.

## Docs

- `mkdocs serve` / `mkdocs build` — the API reference is auto-generated at
  build time by `scripts/gen_ref_pages.py` (crawls `src/` and emits stubs
  under `docs/reference/`). Don't hand-edit `docs/reference/`.

## Existing instruction sources

- `DEVELOPMENT.md` — dev workflow (mostly accurate; the `uv run fusionserve`
  command is broken, see above).
- `README.md` — user-facing, some drift from code (FastAPI claim,
  `pg_app_schema` default). Trust the code.
- `.claude/`, `.kilo/` — session artefacts from other agent tools; not
  authoritative.
