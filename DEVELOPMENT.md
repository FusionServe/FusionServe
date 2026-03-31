# Development Guide

## Prerequisites

- Python **3.14+**
- [`uv`](https://docs.astral.sh/uv/) (package manager and project runner)

---

## Core Workflow with uv

### Install dependencies

Install all dependencies, including the `dev` group (linting, docs, pre-commit):

```bash
uv sync --all-groups
```

### Run the application

```bash
uv run fusionserve
```

Or using uvicorn directly with hot-reload:

```bash
uv run uvicorn fusionserve.main:app --reload --port 8001
```

### Run tests

```bash
uv run pytest
```

### Linting and formatting

FusionServe uses [Ruff](https://docs.astral.sh/ruff/) for both linting and formatting.

```bash
# Lint (with auto-fix)
uv run ruff check --fix .

# Format
uv run ruff format .
```

### Pre-commit hooks

Install the git hooks so that linting, formatting, and `uv.lock` consistency are enforced automatically on every commit:

```bash
uv run pre-commit install
```

Run hooks manually against all files:

```bash
uv run pre-commit run --all-files
```

The hooks configured in [`.pre-commit-config.yaml`](.pre-commit-config.yaml) include:

| Hook | Purpose |
|------|---------|
| `trailing-whitespace` | Strip trailing whitespace |
| `end-of-file-fixer` | Ensure files end with a newline |
| `check-yaml` / `check-json` | Validate YAML and JSON syntax |
| `uv-lock` | Keep `uv.lock` in sync with `pyproject.toml` |
| `ruff-check` | Lint Python code |
| `ruff-format` | Format Python code |

### Add a dependency

```bash
# Runtime dependency
uv add <package>

# Development-only dependency
uv add --group dev <package>
```

After adding, commit both `pyproject.toml` and the updated `uv.lock`.

---

## Documentation Development with MkDocs

The documentation is built with [MkDocs Material](https://squidfunk.github.io/mkdocs-material/) and configured in [`mkdocs.yaml`](mkdocs.yaml).

### Serve docs locally

Start the live-reloading development server:

```bash
uv run mkdocs serve
```

The server watches both the source code (`src/fusionserve/`) and the `docs/` directory and automatically reloads when either changes.

Open [http://127.0.0.1:8000](http://127.0.0.1:8000) in your browser.

### Build docs

Produce a static build in `docs/_build/`:

```bash
uv run mkdocs build
```

### Documentation structure

```
docs/
├── index.md                  # Introduction / home page
├── markdown.md               # Markdown style reference
├── features/                 # One page per feature
│   ├── automatic_api_generation.md
│   ├── compression.md
│   ├── filtering.md
│   ├── graphql_api.md
│   ├── observability.md
│   ├── openapi_docs.md
│   ├── pagination.md
│   ├── rest_api.md
│   ├── role_based_security.md
│   └── smart_comments.md
└── _static/                  # Static assets (images, etc.)

scripts/
└── gen_ref_pages.py          # Auto-generates API reference pages from docstrings
```

### Auto-generated API reference

The [`scripts/gen_ref_pages.py`](scripts/gen_ref_pages.py) script (executed by the `gen-files` MkDocs plugin at build/serve time) crawls `src/` and generates a `reference/` section under `docs/` on-the-fly. Each Python module gets a Markdown page that delegates rendering to `mkdocstrings`.

Docstrings must follow the **Google style**:

```python
def my_function(param: str) -> int:
    """Short one-line summary.

    Args:
        param: Description of the parameter.

    Returns:
        Description of the return value.

    Raises:
        ValueError: If param is invalid.
    """
```

Private members (prefixed with `_`) are excluded from the generated reference by default (see the `filters: ["!^_"]` option in [`mkdocs.yaml`](mkdocs.yaml)).

### MkDocs plugins in use

| Plugin | Purpose |
|--------|---------|
| `search` | Full-text search |
| `mkdocstrings` | Renders Python docstrings as API reference |
| `gen-files` | Runs `scripts/gen_ref_pages.py` at build time |
| `literate-nav` | Builds navigation from `SUMMARY.md` files |
