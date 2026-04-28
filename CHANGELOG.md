# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Security

- Replaced `assert settings.jwt_issuer is not None` in `auth._resolve_jwks_url`
  with an explicit `RuntimeError`. Asserts are stripped under `python -O` and
  must not be used for runtime validation.
- Narrowed the catch-all `except Exception` in `auth.verify_and_decode` to
  `(httpx.HTTPError, jwt.PyJWKClientError)` so genuine programmer errors are
  no longer masked as 401s.
- Added `.env.example` and confirmed `.env` is git-ignored so contributors do
  not accidentally commit credentials.

### Added

- `models.RecordNotFoundError` — typed exception raised by the GraphQL `pk`,
  `update`, and `delete` resolvers in place of bare `raise Exception("not found")`.
- `settings.default_page_size` (default 50) — REST and GraphQL list endpoints
  use this when the client doesn't specify a `limit`.
- REST `create_input_model` — POST bodies no longer require the client to
  pass `null` for server-defaulted columns (PK serial, `created_at`, etc.).
  Non-nullable columns without any default remain required.
- `_set_resolver_arguments` helper in `graphql` to make the
  Strawberry-private-API pattern of overwriting `base_resolver.arguments`
  visible and grep-able.
- Pytest configuration (`asyncio_mode = "auto"`, strict markers, `integration`
  marker), `pytest-asyncio`, and `testcontainers[postgres]` dev deps.
- Unit tests: `parse_comments`, `pydantic_field_from_column`,
  `_make_hashable`, `apply_where`, `apply_order_by`,
  `columns_from_selections`, `verify_and_decode` (with a mocked PyJWKClient).
- Integration test scaffold (`tests/test_integration_introspection.py`)
  gated by `RUN_INTEGRATION=1`, plus a matching CI job that spins up a
  PostgreSQL container via testcontainers.
- `CHANGELOG.md`, `CONTRIBUTING.md`, `SECURITY.md`, `.github/dependabot.yml`.
- `src/fusionserve/py.typed` marker so downstream consumers see the package's
  type hints.

### Changed

- REST list resolvers reject `_limit` greater than `settings.max_page_length`
  with a 400, instead of silently honouring arbitrarily large pages.
- GraphQL list resolvers reject `limit <= 0`, `offset < 0`, and
  `limit > settings.max_page_length` with a typed error. Default `limit`
  is now `settings.default_page_size`.
- `models.AdvancedFilter.examples` is now a list (Pydantic 2 expects a
  sequence; the previous string value was silently converted).
- `graphql.build()` is re-entrant: the Strawberry mapper, `Query`, and
  `Mutation` are constructed inside the function so repeated calls (tests,
  hot reload) start from a clean state.
- f-string `_logger.error/debug` calls converted to `%`-style so future
  `Ruff G` rules can enforce this and so log handlers can format lazily.
- `Litestar` lifespan now disposes the async engine on shutdown so SQLAlchemy
  no longer logs a warning at interpreter exit.
- `persistence.introspect` now scopes the synchronous psycopg engine to the
  function and disposes its connection pool before returning.
- `pyproject.toml`:
  - `version` normalised to `0.1.0` (PEP 440).
  - License declaration migrated to PEP 639 (`license = "MIT"` +
    `license-files = ["LICENSE.txt"]`).
  - Classifiers expanded.
  - `psycopg` + `psycopg-binary` collapsed into `psycopg[binary]`.
  - `uvicorn[standard]` no longer duplicated in the dev group.
  - `httpx` declared explicitly (it was previously a transitive dependency
    even though `auth.py` imports it directly).
  - `icecream` moved from runtime to dev group.
  - `suds` removed (unused).
  - Legacy `platforms = ["any"]` dropped.
  - Broken `[project.scripts]` entry pointing at non-existent
    `fusionserve.main:run` removed. Use `uvicorn fusionserve.main:app`.
  - `uv_build` upper bound raised to `<0.11.0`.
- CI's lint-test job runs only unit tests (`-m "not integration"`); a new
  `integration-test` job runs `-m integration` with `RUN_INTEGRATION=1` so
  Docker-backed tests get coverage on every push.

### Tooling

- Ruff `select` expanded to add `G`, `LOG`, `ASYNC`, `PT`, `RET`, `C4`,
  `PIE`, `TID`, and `RUF`. The new rules surfaced (and we fixed) several
  unused `noqa` directives, dead `else` branches after `return`, EN-DASH
  characters in docstrings, and a `RUF012` mutable class attribute on the
  generated REST controller's `tags`.
- `src/fusionserve/di.py` shrank from ~720 lines of vendored
  `advanced_alchemy` provider helpers down to a 30-line re-export of
  upstream symbols. Internal callers (and any external code that used
  ``from fusionserve.di import …``) keep working.
- `inflect` engine is now constructed once in `persistence` and re-used
  by `rest` and `graphql`. The three duplicate engines (and their
  duplicate ``classical(names=0)`` calls) are gone.

## [0.1.0] - YYYY-MM-DD

Initial public version.
