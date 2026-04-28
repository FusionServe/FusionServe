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
- `CHANGELOG.md`, `CONTRIBUTING.md`, `SECURITY.md`, `.github/dependabot.yml`.
- `src/fusionserve/py.typed` marker so downstream consumers see the package's
  type hints.

### Changed

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

## [0.1.0] - YYYY-MM-DD

Initial public version.
