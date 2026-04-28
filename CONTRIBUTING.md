# Contributing to FusionServe

Thanks for considering a contribution. This document is the short version;
the deeper, agent-oriented working notes live in [`AGENTS.md`](AGENTS.md), and
day-to-day developer workflow lives in [`DEVELOPMENT.md`](DEVELOPMENT.md).

## Toolchain

- **Python 3.14+** is required.
- We use [`uv`](https://docs.astral.sh/uv/) as the package manager and runner.
  Do not invoke `pip`, `poetry`, or `python -m venv` directly.

```bash
uv sync --all-groups        # install runtime + dev deps
uv run pre-commit install   # one-time, optional
```

## Local checks (mirror CI)

CI fails on any of these. Run them locally before opening a PR:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
```

For local iteration:

```bash
uv run ruff check --fix .
uv run ruff format .
```

## Running the app

```bash
uv run uvicorn fusionserve.main:app --reload --port 8001
```

The app needs a reachable PostgreSQL instance on startup — schema
introspection happens in the Litestar lifespan and the process will not
come up without the database. See README for the required PostgreSQL
privileges.

## Style

- 120-character line length (Ruff-enforced).
- Public functions use **Google-style** docstrings — `mkdocstrings`
  generates the API reference from them.
- Identifiers prefixed with `_` are treated as private and excluded from
  generated documentation.

## Lockfile discipline

The pre-commit `uv-lock` hook will regenerate `uv.lock` whenever
`pyproject.toml` changes. Always commit `uv.lock` in the same commit as the
`pyproject.toml` change. CI uses `uv sync --frozen`; a drifted lockfile
breaks CI.

## Tests

The test suite is small and primarily a smoke test today. New features are
expected to come with at least unit tests. Integration tests that require a
live PostgreSQL connection should be skipped by default and gated behind an
explicit opt-in (e.g. `RUN_INTEGRATION=1`).

## Commits & PRs

- Conventional, descriptive commit subjects (no fixed prefix is enforced).
- Keep PRs focused; large mechanical refactors should be in their own PR.
- Mention any breaking change in the PR description so it lands in the
  changelog.
