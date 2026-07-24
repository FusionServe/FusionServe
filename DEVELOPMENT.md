# Development Guide

## Prerequisites

- Python **3.14+**
- [`uv`](https://docs.astral.sh/uv/) (package manager and project runner)
- [`pnpm`](https://pnpm.io) (JS package manager for the bundled UI).
  Node **24.15+** (required by the Angular v22 CLI) with `corepack
  enable` auto-activates the pinned pnpm version from
  `ui/package.json`'s `packageManager` field — no global `pnpm`
  install required.

> The JS toolchain is **pnpm-only**. Do not run `npm`, `npx`, `bun`,
> `bunx`, or `yarn` inside `ui/` — install with `pnpm install`, run
> scripts with `pnpm run …`, and commit `pnpm-lock.yaml` alongside
> any `package.json` change. Same discipline as `uv.lock` on the
> Python side.

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

Litestar serves the SPA via a static-files router (built in
`fusionserve.ui`) reading from `src/fusionserve/web/dist`. The
directory must contain a built `index.html` (plus the hashed chunks
it references), otherwise requests to `settings.ui_path` (default
`/api/-/`, derived from `base_path`) 404 at request time. Run
`pnpm run build` in `ui/` to populate it; the dev workflow below
avoids needing a build at all by serving the SPA from a standalone
Angular dev server.

### Frontend dev workflow

```bash
# One-time install
cd ui
pnpm install

# Two-terminal dev workflow:
#  - Terminal A: backend
uv run uvicorn fusionserve.main:app --reload --port 8001 --log-config=logging.yaml
#  - Terminal B: Angular dev server (proxies /api/* to :8001)
cd ui && pnpm run dev
# Then visit http://localhost:5173/

# Production-style build (writes to ../src/fusionserve/web/dist):
cd ui && pnpm run build

# Type-check only:
cd ui && pnpm run typecheck
```

The Angular dev server's proxy (configured in `ui/proxy.conf.json`)
forwards every `/api/*` and `/.well-known/*` request — REST CRUD,
GraphQL, OpenAPI surfaces, the client-config document — to the backend
on `:8001`. In dev the SPA is reached at the dev-server root
(`http://localhost:5173/`), **not** at `<ui_path>`; the app uses
browser-history (path) routing and derives its router base from
`document.baseURI` (via `APP_BASE_HREF`), so deep links reload cleanly
in both dev (Angular's history fallback) and prod (the base-href
injecting index handler). Hitting `http://localhost:5173/api/` in dev
follows the proxied 302 to `<ui_path>`, which then serves the
*prebuilt* `index.html` from `web/dist` rather than the HMR-backed
one — visit the dev-server root directly to stay in HMR.

### URL layout cheat-sheet

| Path | Owner | Notes |
|------|-------|-------|
| `/api/` | `RedirectRenderPlugin` (`fusionserve.ui`) | 302 redirect to `settings.ui_path` (default `/api/-/`). |
| `/api/openapi.json` | Litestar OpenAPI router | Auto-registered JSON handler (no explicit `JsonRenderPlugin`; the upstream "fallback" path provides it). |
| `/api/swagger` | `SwaggerRenderPlugin` | Swagger UI (the SPA's OpenAPI page uses bundled Stoplight Elements instead). |
| `/api/scalar` | `ScalarRenderPlugin` | Scalar API reference. |
| `/api/v1/<table>` | `fusionserve.rest` | Dynamically generated REST CRUD endpoints (`v1` is the API version). |
| `/api/graphql` | `fusionserve.graphql` | Strawberry GraphQL endpoint + GraphiQL on `GET` (the SPA's GraphQL page embeds Altair instead). |
| `<ui_path>assets/...` | Assets static-files router (`fusionserve.ui`) | Angular browser bundle (hashed chunks, `index.html`, embedded Altair/Stoplight assets). |
| `<ui_path>` (default `/api/-/`, and any `<ui_path>/{path:path}`) | Base-href-injecting index handler (`fusionserve.ui`) | Angular SPA index + deep-link fallback. `<base href>` rewritten to `<ui_path>assets/`; router base derived one level up via `APP_BASE_HREF`. Wired via `Settings.ui_path`. |
| `/metrics` | Litestar Prometheus | Standard Prometheus exposition. |

`Settings.ui_path` derives from `Settings.base_path` (default
`f"{base_path}/-/"` → `/api/-/`) via the `_derive_ui_path` validator
in `fusionserve.config`. Setting `UI_PATH=…` overrides it verbatim.
The trailing-slash form is canonical: `<base_path>/-` (no slash) is
matched by the OpenAPI router's `<base_path>/{path:str}` not-found
handler and 404s; the `<base_path>/` -> `<ui_path>` redirect always
emits the slash so users on the normal entry path see the SPA.

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
