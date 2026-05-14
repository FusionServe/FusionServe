# Development Guide

## Prerequisites

- Python **3.14+**
- [`uv`](https://docs.astral.sh/uv/) (package manager and project runner)
- [`bun`](https://bun.sh) (JS runtime + package manager for the bundled UI)

> The JS toolchain is **bun-only**. Do not run `npm`, `npx`, `pnpm` or
> `yarn` inside `ui/` — install with `bun install`, run scripts with
> `bun run …`, and commit `bun.lock` alongside any `package.json`
> change. Same discipline as `uv.lock` on the Python side.

---

## Core Workflow with uv

### Install dependencies

Install all dependencies, including the `dev` group (linting, docs, pre-commit):

```bash
uv sync --all-groups
```

### Run the application

```bash
uv run uvicorn fusionserve.main:app --reload --port 8001
```

> Do **not** run `uv run fusionserve` — the `[project.scripts]` entry
> points at a non-existent module and only `ImportError`s.

The Litestar Vite plugin in SPA mode loads the bundled `index.html` at
startup. Either pre-build the SPA once with `bun run build` (in `ui/`),
or set `VITE_DEV_MODE=True` and run the bun-driven Vite dev server in
parallel — see the next section.

### Frontend dev workflow

```bash
# One-time install
cd ui
bun install

# Two-terminal dev workflow with HMR (one-port via litestar-vite proxy):
#  - Terminal A: backend with VITE_DEV_MODE=True
VITE_DEV_MODE=True uv run uvicorn fusionserve.main:app --reload --port 8001
#  - Terminal B: bun-driven Vite dev server (proxied through the backend)
cd ui && bun run dev

# Production-style build (writes to ../src/fusionserve/web/dist):
cd ui && bun run build

# Type-check only:
cd ui && bun run typecheck
```

### URL layout cheat-sheet

| Path | Owner | Notes |
|------|-------|-------|
| `/api/` | `RedirectRenderPlugin` (`fusionserve.ui`) | 302 redirect to `settings.ui_path`. |
| `/api/openapi.json` | Litestar OpenAPI router | Auto-registered JSON handler (no explicit `JsonRenderPlugin`; the upstream "fallback" path provides it). |
| `/api/swagger` | `SwaggerRenderPlugin` | Swagger UI. |
| `/api/scalar` | `ScalarRenderPlugin` | Scalar API reference. |
| `/api/v1/<table>` | `fusionserve.rest` | Dynamically generated REST CRUD endpoints (`v1` is the API version). |
| `/api/graphql` | `fusionserve.graphql` | Strawberry GraphQL endpoint + GraphiQL on `GET`. |
| `/-/` (and `/-/{path}`) | `litestar-vite` SPA handler | React SPA index. Wired via `Settings.ui_path`. |
| `/-/assets/...` | `litestar-vite` static router | Hashed JS/CSS chunks. Wired via `Settings.ui_assets_path`. |
| `/metrics` | Litestar Prometheus | Standard Prometheus exposition. |

The asset URL (`Settings.ui_assets_path`, default `/-/assets/`) lives
at a top-level prefix outside `settings.base_path` so the OpenAPI
router's auto-registered `<base_path>/{path:str}` not-found handler
cannot shadow asset requests. The matching literal is hard-coded in
`ui/vite.config.ts` (`assetUrl`); changing one without the other
breaks asset serving.

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
