# Spike: Evaluate switching GraphQL generation from strawberry-sqlalchemy to strawberry-orm

**Date:** 2026-06-04
**Status:** Design approved — ready for implementation plan
**Branch:** `strawberry-orm`

## Background

FusionServe builds its entire GraphQL schema dynamically at runtime in
`graphql.build()` by iterating SQLAlchemy automap classes. It depends on
[`strawberry-sqlalchemy-mapper`](https://pypi.org/project/strawberry-sqlalchemy-mapper/)
for three things:

- ORM-class → Strawberry type mapping (`mapper.type(orm_class)`,
  `mapper._convert_column_to_strawberry_type`, `mapper.mapped_types`,
  `mapper.finalize()`),
- relationship resolution via `StrawberrySQLAlchemyLoader` (wired with a
  per-request `set_role` `async_bind_factory` for row-level security),
- FK-aware column loading.

Everything else (the `Where`/`OrderBy`/`Input`/`Patch` input-type generators,
the eight CRUD resolvers, the PG-function resolvers, and RLS role switching) is
first-party code that does not depend on the mapper.

The upstream project is in maintenance limbo: issue
[#180](https://github.com/strawberry-graphql/strawberry-sqlalchemy/issues/180)
(Jun 2024) is a "we have no maintainer, let's converge with strawberry-django"
thread, and we have hit a bug in practice. The Strawberry org's successor,
[`strawberry-orm`](https://pypi.org/project/strawberry-orm/) (0.13.0, May 2026),
is a backend-agnostic schema generator supporting SQLAlchemy
(`StrawberryORM.for_sqlalchemy(dialect=, session_getter=)`), Python ≥3.12,
strawberry-graphql ≥0.311. **It is labeled Dev Status 3 - Alpha — "expect
breaking changes and incomplete APIs."**

A key architectural mismatch: strawberry-orm's documented API is *declarative*
(hand-written `@orm.type(Model)` classes with `auto` fields, plus generated
`orm.filter()`/`orm.order()`/`orm.input()` and a query optimizer that replaces
the dataloader). FusionServe generates everything dynamically from
introspection with zero hand-written types. The central question this spike
answers is whether strawberry-orm's builders can be driven *programmatically*
the way the introspection loop needs.

## Goal

A time-boxed, in-place feasibility spike on the `strawberry-orm` branch that
rewrites `graphql.build()` to use strawberry-orm driven dynamically from
automap introspection (**Approach A — dynamic decorator translation**),
covering **read + write parity** using strawberry-orm's **native** filter /
order / input shapes. PG-function custom queries are deliberately deferred.

The spike produces a **go / no-go recommendation**.

### Decisions taken during design

- **Approach A** (dynamic decorator translation) over a hybrid (keep
  first-party builders) or a probe-first variant.
- **Adopt strawberry-orm's native GraphQL API shapes** (recursive `@oneOf`
  filter trees, list-based `order`, generated `input`/`partial`). The public
  GraphQL API surface is allowed to change.
- **Read + write parity** in scope; PG-function custom queries out of scope.
- **In-place rewrite** on the existing branch (no parallel module).
- **Fail-fast primitive probes dropped** (we are already on a dedicated branch).

### Success criteria (drive the go/no-go)

1. **Dynamic type generation** — `orm.type(orm_class)` accepts synthesized
   classes built in a loop from automap columns + relationships, with
   plural-table → singular-type naming preserved.
2. **Relationship traversal** — nested queries (e.g. `{ orders { user { name } } }`)
   resolve correctly via `orm.schema()`'s optimizer, with no N+1 explosion,
   replacing `StrawberrySQLAlchemyLoader`.
3. **RLS correctness** — per-request `set_role` is honored for every query,
   mutation, and nested load. *A leak here is an automatic no-go.*
4. **Write parity** — create / update / delete (single + many) work via
   `orm.input` / `orm.partial`-derived inputs.
5. **Alpha friction** — a qualitative log of bugs / missing APIs / workarounds,
   and whether each had a viable workaround.

## Design

### 1. Dependencies

- Add `strawberry-orm[sqlalchemy]` to `pyproject.toml` dependencies; run
  `uv sync`; commit `uv.lock` in the **same commit** (the pre-commit
  `uv-lock` hook and CI `uv sync --frozen` require this).
- Keep `strawberry-sqlalchemy-mapper` installed for the duration of the spike
  to allow cheap diff / revert. Remove it only if the spike becomes a real
  migration.
- Up-front compatibility check: confirm a single resolved `strawberry-graphql`
  version satisfies both strawberry-orm (≥0.311.0) and the existing mapper
  simultaneously. A pin conflict is itself a go/no-go signal.

### 2. Rewrite `graphql.build()`

Keep the outer contract unchanged: introspect → iterate `Base.classes` →
return a Litestar GraphQL controller mounted at `<base_path>/graphql`.

**Module-level setup**

- Construct one `orm = StrawberryORM.for_sqlalchemy(dialect="postgresql",
  session_getter=<context session>)` per `build()` call, preserving the
  "isolated per build" property required for test reload / dev hot-reload /
  multiple apps in one process.
- Map production knobs onto existing settings: `default_query_limit` ←
  `settings.default_page_size`; `max_filter_depth` ← 10 (the spirit of the
  current `_MAX_WHERE_DEPTH`); set `max_filter_branches` / `max_in_list_size`
  to sensible caps; `enable_optimizer=True`.

**Per-table loop (replaces the mapper block)**

Because filter/order `object` traversal only includes a relation if the target
model's filter/order type is already registered, and automap iteration order is
not dependency-sorted, the loop runs in **two passes**:

- **Pass 1** — for every table, generate and register `orm.filter(orm_class)`
  and `orm.order(orm_class)`.
- **Pass 2** — for every table:
  - Synthesize a class whose `__annotations__` map each column to `auto` and
    each relationship to the related generated type (`list[T]` or `T`), then
    apply `orm.type(orm_class, name=<PascalSingular>)(cls)`. This is the direct
    analogue of today's `mapper.type(orm_class)(type(...))`.
  - Preserve naming: plural table → singular Pascal type name; singular
    camelCase FK relationship field names (port `_rename_fk_fields` — relations
    may need explicit field naming since strawberry-orm derives its own).
  - Apply column / table descriptions from `SmartComment` if strawberry-orm
    exposes a description hook (`orm.field(description=...)` / type-level). If
    no hook exists, record it in the friction log.
  - Generate `orm.input(orm_class)` and `orm.partial(orm_class)`.
  - Views stay read-only (no input / mutation surface), matching today's
    `is_view` guard.

**Query root**

- List field via `orm.field()` / `orm.connection()`.
- A pk-lookup field via a custom `@orm.field` resolver filtering by PK args
  (strawberry-orm does not generate by-pk getters).
- Keep `current_user`.

### 3. Mutations (write parity)

Replace the eight CRUD resolvers, keeping field names and RETURNING-based
behavior where it survives the new model:

- **create / createMany** — `@strawberry.mutation` resolvers taking
  `orm.input(orm_class)` (single) and `list[...]` (many); keep the single
  `INSERT ... RETURNING` statement for createMany.
- **update / updateMany** — take `orm.partial(orm_class)` for the patch.
  Single: by PK args. Many: a `where` argument using the native `orm.filter`
  type, translated to a SQLAlchemy condition. **Preserve the guardrail**: an
  empty / None resolved filter raises `ValueError` to block accidental
  table-wide writes.
- **delete / deleteMany** — same pattern, `DELETE ... RETURNING`, same
  empty-filter guardrail on deleteMany.
- Every mutation resolver uses the context session that already had `set_role`
  applied (see RLS below).
- Open question the spike answers: does strawberry-orm's optimizer / return-type
  machinery let mutation resolvers return ORM instances and still resolve
  nested selections under the mutation payload? Record in the friction log if
  not.

### 4. RLS integration (security-critical)

This is the highest-risk correctness item. Today every resolver opens its own
`async_session()` and calls `set_role`; the relationship loader does the same
via `async_bind_factory`. strawberry-orm's optimizer instead pulls a single
session from `session_getter(info)`.

- `custom_context_getter` opens one `AsyncSession`, calls
  `await set_role(session, request.user)`, and stores it on the context.
  `session_getter=lambda info: info.context.session`.
- The optimizer, mutation resolvers, and pk resolver all share that one
  role-scoped session, so nested loads inherit the role automatically
  (replacing `StrawberrySQLAlchemyLoader`'s per-batch bind).
- **Session lifecycle**: the context session must stay open for the whole
  request and be closed / rolled back afterward. Strawberry's `context_getter`
  is a plain async function with no teardown hook, so teardown is owned by a
  Litestar dependency / middleware hook (e.g. an injected session dependency or
  an `after_response` hook). The spike picks whichever Litestar offers cleanly;
  the chosen mechanism is itself a documented finding.
- **Validation**: an integration test asserts an RLS-protected table returns
  different rows for anonymous vs. authenticated roles, **including through a
  nested relationship load** — the exact leak class strawberry-orm's docs warn
  about ("scope every model type clients can reach").

### 5. Validation & what we measure

One `integration`-marked test module (testcontainers PG, gated on
`RUN_INTEGRATION=1`, matching the existing pattern) seeds a small schema:
a plural-named parent/child pair with an FK, a view, and an RLS policy keyed on
`current_setting('role')`.

The test exercises and asserts:

- **Read** — list (with a native `filter` and `order`), pk lookup, 2-level
  nested relationship.
- **Write** — create, createMany, update, updateMany, delete, deleteMany,
  including the empty-`where` guardrail raising on updateMany / deleteMany.
- **RLS** — anonymous vs. authenticated row visibility, including through a
  nested load.
- **N+1** — capture the SQL statement count for the nested query (via a
  SQLAlchemy event / echo) and assert it is bounded.

Alongside the test, maintain a short qualitative **friction log** (markdown
notes) recording every alpha bug / missing API / workaround. Both feed the
recommendation.

### 6. Deliverable: go/no-go writeup

The spike ends with a written recommendation appended to this spec covering:

- Pass / fail against each of the five success criteria.
- The friction log (severity + workaround availability per issue).
- Effort estimate for a full migration (PG-functions, descriptions, the public
  API-shape change for clients, removal of the old mapper) if the result is a
  "go".
- An explicit recommendation: **go-now / go-later (wait for beta) / no-go**,
  with reasoning tied to strawberry-orm's alpha status. An RLS leak is an
  automatic no-go.

## Out of scope

- PG-function custom queries (ROW / SET / SCALAR) — deferred to a follow-up if
  the spike is a "go".
- Removing `strawberry-sqlalchemy-mapper` from dependencies.
- Building a compatibility layer to preserve the current Hasura-style GraphQL
  API shape (the native shape is adopted instead).

## Spike outcome (2026-06-04): findings & recommendation

Executed on branch `strawberry-orm`. `graphql.build()` was rewritten in place
on `strawberry-orm 0.13.0` (read + write paths); validation is in
`tests/test_integration_strawberry_orm.py` (15 integration tests, testcontainers
PG, all green). Environment note: run via a Podman socket forwarded over SSH
(`DOCKER_HOST=unix:///tmp/podman-fs.sock`, `TESTCONTAINERS_RYUK_DISABLED=true`,
`TESTCONTAINERS_HOST_OVERRIDE=<vm-ip>`).

### Result against the five success criteria

1. **Dynamic type generation — PASS (with friction).** `orm.type()` is driven
   dynamically over automap classes by synthesizing one annotation per column +
   relationship. Cyclic relationships (author↔book) work via pre-created bare
   classes in a shared module with `__orm_filter__`/`__orm_order__` pre-seeded,
   decorated in place. Singular PascalCase type names preserved via `name=`.
2. **Relationship traversal — PASS.** Both to-many (`booksCollection`) and
   to-one (`authors` scalar) resolve via the optimizer. The nested
   author→books query issues a bounded number of SELECTs (≤4 for 2 authors —
   no N+1).
3. **RLS correctness — PASS (security-critical).** A single per-request
   role-scoped `AsyncSession` exposed via `session_getter` is reused by the
   backend for every query, optimizer load, and nested relation load.
   Anonymous vs. authenticated visibility is correct at the root **and through
   a nested relation load** (no leak). Session teardown is clean: the
   `context_getter` is an async generator wired through Litestar's `Provide`.
4. **Write parity — PASS.** create / createMany / update / updateMany / delete /
   deleteMany work via `orm.input` / `orm.partial` / `orm.filter`,
   RETURNING-based; the empty-`where` guardrail is preserved.
5. **Alpha friction — documented below.**

### Friction log

| # | Finding | Severity | Workaround |
|---|---------|----------|-----------|
| 1 | Backend type map downgrades `UUID`→`str` and `JSON`→`str`. Also, annotating with concrete `python_type` exposes JSON/JSONB as `dict`, which strawberry rejects (`Unexpected type '<class 'dict'>'`) and crashes schema build for any table with a JSON column (e.g. `users`). | Med | Annotate columns with concrete `python_type` instead of `auto`, and map `dict`/`list` python types to the Strawberry `JSON` scalar (done; covered by a JSONB regression test). |
| 2 | `type()` does **not** auto-include columns; every field must be declared. | Low | Synthesize annotations from `__table__.columns` + `__mapper__.relationships` (done). |
| 3 | No per-field rename on the dynamic `type()` path → relationships use automap default names (`booksCollection`, scalar `authors`) instead of the old singularized `books` / `author`. **Public API change.** | Med | **Resolved (WS2):** renamed at the automap-callback layer so to-one relations are singular (`author`) and to-many are the plural source-table name (`books`). REST is unaffected (it doesn't use relationships). |
| 4 | No description hook on dynamic `type()` → column/table SmartComment descriptions are lost on output types. | Med | Post-process `__strawberry_definition__.fields`, or hand-build fields. Not done. |
| 5 | `from __future__ import annotations` stringifies resolver return hints, which strawberry can't resolve for locally-scoped generated types. | Low | Set `field.type_annotation` explicitly (done). |
| 6 | A bare `list` annotation on a resolver param crashes `strawberry.mutation`. | Low | Use `Any` / concrete type; real arg type set via `_set_resolver_arguments` (done). |
| 7 | To-one relations emit a "resolves lazily" `UserWarning`. | Low | Advisory only; traversal works. Optionally add `load=`/`disable_optimization`. |
| 8 | Spike reaches into private backend attrs (`_filter_registry`, `_order_registry`). | Med | No public accessor in 0.13.0; fragile across alpha releases. |
| 9 | Mutations `commit()` then `refresh()` / could resolve post-commit work in a **new** transaction where the transaction-local `SET ROLE` no longer applies. | **High (potential RLS gap)** | **Resolved (WS1):** an `after_begin` hook re-applies the transaction-local role on every transaction the request's session opens (initial + post-commit), keeping pooled connections role-free. Proven by `test_ws1_role_reapplied_on_each_transaction`. |
| 12 | Selecting a **relation inside a mutation payload** (e.g. `updateAuthor(...) { booksCollection { ... } }`) triggers an async lazy-load and fails with `greenlet_spawn has not been called`, because mutation resolvers return raw ORM instances the optimizer never eager-loaded. | Med | Spike limitation: mutation payloads should select scalar columns only. A full migration would eager-load (or re-issue an optimized SELECT for) the returned rows so payload relations resolve. Not yet implemented. |
| 10 | Cyclic-relationship handling relies on `strawberry.type` mutating the bare class in place. | Med | Works in 0.13.0; revalidate on upgrade. |
| 11 | Nested relation (`object`) filters are wired for only **one direction** of a bidirectional relationship. `orm.filter()` reads related filters from the registry at build time; SQLAlchemy automap creates relationships in both directions for every FK (a 2-cycle), so only the model registered *second* gets the `object` key. Example: `booksFilter` can filter by `author`, but `authorsFilter` cannot filter by `books`. A topological sort does **not** help (the pair is mutually dependent), and re-registering to complete both directions produces duplicate same-named filter types. Worse, `automap`'s class iteration order is **not stable across processes**, so which direction is wired could flip between deploys. | Med | `build()` now iterates `base.classes` sorted by table name, so the wired direction is **deterministic** (alphabetically-first model registered first → its FK-target's filter gets the `object` key). Accepted as a library limitation in 0.13.0 and pinned by `test_filter_object_traversal_cyclic_limitation`. No clean workaround for full bidirectional traversal; revisit when the library supports it. |

### Effort estimate for a full migration (if "go")

- Reimplement PG-function custom queries (ROW/SET/SCALAR) — **out of spike scope**.
- Restore singularized relationship field names (hand-built relation fields).
- Restore SmartComment descriptions on output types.
- Decide the pagination story (old `PaginationWindow`/`totalCount` vs. relay
  connections via `orm.connection()` vs. plain lists).
- Resolve the post-commit RLS gap for mutation payloads (#9) — **must-fix**.
- Remove `strawberry-sqlalchemy-mapper`; update `docs/features/graphql_api.md`.
- Communicate/version the **public GraphQL API shape change** (native `@oneOf`
  filters, list/connection shape, renamed relationship fields) to clients.

Estimated 1–2 focused weeks for parity + the must-fix, excluding client migration.

### Recommendation: **GO-LATER (conditional)**

Core feasibility is proven on all five criteria with **no hard blocker and no
RLS leak** in the read path — the security-critical concern maps cleanly onto
strawberry-orm's "reuse the caller's session" model. However:

- strawberry-orm is **alpha** ("expect breaking changes"), and the spike depends
  on private attributes (#8) and in-place decoration behaviour (#10).
- One **must-fix** correctness gap remains for mutation payloads (#9).
- Several API-visible regressions (naming #3, descriptions #4) need rework.

Commit to the direction and write a full migration plan, but **gate the
production cutover on strawberry-orm reaching beta/stable** and on resolving
#9, #3, and #4. Keep this branch as a living proof in the interim. Re-run the
integration suite on each strawberry-orm upgrade to catch alpha churn.

## References

- Spike branch implementation: `src/fusionserve/graphql.py` (this branch)
- Spike tests: `tests/test_integration_strawberry_orm.py`
- Current implementation: `src/fusionserve/graphql.py`
- RLS role switching: `persistence.set_role`, `persistence.async_session`
- strawberry-sqlalchemy maintenance thread:
  <https://github.com/strawberry-graphql/strawberry-sqlalchemy/issues/180>
- strawberry-orm: <https://pypi.org/project/strawberry-orm/>
