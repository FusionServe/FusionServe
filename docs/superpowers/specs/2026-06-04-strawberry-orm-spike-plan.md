# Implementation Plan: strawberry-orm spike

Companion to `2026-06-04-strawberry-orm-spike-design.md`. Execute phases in
order; each phase ends at a verifiable checkpoint. Stop and record a finding
(then continue or declare no-go) whenever an alpha showstopper appears.

CI parity to run before any push (from `AGENTS.md`):

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
```

Integration work runs with `RUN_INTEGRATION=1` and `testcontainers` (already a
dev dependency).

## Phase 0 — Dependency bring-up

- [ ] Add `strawberry-orm[sqlalchemy]` to `pyproject.toml` `dependencies`.
- [ ] `uv sync`; confirm a single `strawberry-graphql` version resolves for both
      strawberry-orm (≥0.311.0) and `strawberry-sqlalchemy-mapper`. If it does
      not resolve, record the conflict — that alone is a strong no-go signal.
- [ ] Commit `pyproject.toml` + `uv.lock` together.
- **Checkpoint:** `uv sync` succeeds; `uv run python -c "import strawberry_orm"`
      works; existing `uv run pytest -q` still green (unit suite imports nothing
      that triggers introspection).

## Phase 1 — Integration harness & fixtures

- [ ] Add an `integration`-marked test module (e.g.
      `tests/test_integration_strawberry_orm.py`) that stands up PG via
      testcontainers and creates the seed schema:
  - plural-named parent/child pair with an FK (e.g. `authors`, `books`),
  - one view,
  - an RLS policy keyed on `current_setting('role')`,
  - the `app_public.current_user_id()` DDL prerequisite if needed.
- [ ] Seed deterministic rows for read/RLS/N+1 assertions.
- **Checkpoint:** harness boots the app against the container and serves
      `<base_path>/graphql` (still on the *old* implementation at this point).

## Phase 2 — Read path rewrite

- [ ] Module-level `StrawberryORM.for_sqlalchemy(...)` per `build()`, with knobs
      mapped from `settings` (`default_query_limit`, `max_filter_depth=10`,
      `max_filter_branches`, `max_in_list_size`, `enable_optimizer=True`).
- [ ] Two-pass loop:
  - Pass 1: register `orm.filter` + `orm.order` for every table.
  - Pass 2: synthesize `auto`-annotated class (columns + relationships), apply
    `orm.type(orm_class, name=PascalSingular)(cls)`; preserve singular type
    names and singular camelCase FK relationship field names; apply
    `SmartComment` descriptions if a hook exists (else log friction).
- [ ] Query root: list via `orm.field()`/`orm.connection()`, pk-lookup via
      custom `@orm.field`, keep `current_user`. Views read-only.
- [ ] Build the schema with `orm.schema(query=...)` (optimizer on).
- **Checkpoint (criteria 1 & 2):** integration test passes list (with native
      `filter`/`order`), pk lookup, and a 2-level nested relationship; nested
      query SQL statement count is bounded (N+1 guard via SQLAlchemy event).

## Phase 3 — RLS integration

- [ ] `custom_context_getter` opens one `AsyncSession`, calls
      `set_role(session, request.user)`, exposes it on context;
      `session_getter=lambda info: info.context.session`.
- [ ] Own session teardown via a Litestar dependency/middleware/`after_response`
      hook; document the mechanism chosen.
- **Checkpoint (criterion 3):** integration test shows anonymous vs.
      authenticated row visibility differs, **including through a nested load**.
      Any leak → automatic no-go; record and stop.

## Phase 4 — Write path rewrite

- [ ] create / createMany via `orm.input` (+ single `INSERT ... RETURNING`).
- [ ] update / updateMany via `orm.partial`; updateMany `where` uses native
      `orm.filter`, translated to a SQLAlchemy condition; preserve empty-filter
      → `ValueError` guardrail.
- [ ] delete / deleteMany via `DELETE ... RETURNING`; same deleteMany guardrail.
- [ ] All mutations use the role-scoped context session.
- **Checkpoint (criterion 4):** integration test passes all six write ops and
      both empty-`where` guardrails; verify nested selections resolve under
      mutation payloads (record friction if not).

## Phase 5 — Synthesis & recommendation

- [ ] Consolidate the friction log (alpha bugs / missing APIs / workarounds,
      with severity).
- [ ] Append the go/no-go writeup to the design spec: pass/fail per the five
      criteria, full-migration effort estimate (PG-functions, descriptions,
      client API-shape change, mapper removal), and an explicit
      **go-now / go-later / no-go** recommendation.
- **Checkpoint:** recommendation committed; branch left in a comparable state
      (old mapper still installed) for review.

## Notes / guardrails

- Do **not** remove `strawberry-sqlalchemy-mapper` during the spike.
- Do **not** add `/v2` or touch the REST surface.
- PG-function custom queries are out of scope; leave `_build_function_resolver`
  and the function loop intact or temporarily disabled, whichever keeps the
  module importable — record the choice.
- Honor repo style: ruff `line-length=120`, Google-style docstrings on public
  functions, plural-table introspection invariant.
