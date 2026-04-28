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

- `tests/` currently only has a smoke test (`test_skeleton.py`) asserting the
  package exposes `__version__`. There is no test DB, no fixtures for
  introspection, no integration coverage. When adding features, assume you
  are writing the first real test of that area — don't expect helpers.
- `pytest` runs without a database because nothing in the test suite imports
  `main` (which triggers introspection). If you add tests that touch `main`,
  `rest.build`, or `graphql.build`, you will need to stand up a PG instance
  or mock the introspection boundary.

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
