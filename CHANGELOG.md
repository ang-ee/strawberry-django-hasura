# Changelog

All notable changes to `strawberry-django-hasura` are documented here. The
format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.1] — 2026-06-26

### Fixed

- **`_like` / `_ilike` now interpret Hasura SQL-`LIKE` patterns** instead of
  matching the pattern as a literal substring. The stock `@refinedev/hasura`
  provider sends `contains` as `_ilike: "%term%"`; the previous mapping looked
  for the literal `%term%` (percent signs and all). Leading/trailing `%` now map
  to portable `contains` / `startswith` / `endswith` lookups, a bare value
  (no `%`) stays a substring shorthand for authored callers, and any richer
  SQL-`LIKE` pattern (`_` wildcard, embedded `%`, `\` escapes) falls back to
  Django's `regex` / `iregex`. Applied on both the model path (`filtering.py`)
  and the in-memory `run_query` evaluator so the two siblings stay in lockstep.

## [0.3.0] — 2026-06-25

### Added

- **`hasura_run_query_resource(...)`** — a read-only Hasura resource whose rows
  come from a caller-supplied **`RowSource`**, not a Django model. It emits the
  same list / `<res>_aggregate { aggregate { count } }` / `<res>_by_pk` SDL as
  `hasura_resource`, sharing the dialect machinery. The aggregate is
  **count-only** (a computed source needs only the row total for pagination, not
  the SQL aggregate compiler). For computed / foreign data with no table.
- **`RowSource` protocol + `InMemoryRowSource`** — the pushdown seam.
  `RowSource.query` / `.count` receive the parsed `where` so a transport-backed
  source can push the predicate down; `InMemoryRowSource` evaluates it in Python
  over a row iterable.
- **`where_matches` / `apply_in_memory`** — the in-memory dialect evaluator (the
  Python sibling of `filtering.where_to_q`): interprets a `<res>_bool_exp` into a
  per-row predicate and applies ordering + paging over a list.

### Changed

- The `<res>_bool_exp` / `<res>_order_by` input assembly and the snake_case wire
  pinning moved to **`inputs.py`**, composed by both `hasura_resource` and
  `hasura_run_query_resource` (no behaviour change to the model path).

## [0.2.0] — 2026-06-24

### Added

- **`hasura_resource(...)`** — a one-call declarative builder that assembles the
  *whole* Hasura surface for a model (the `<res>` list, `<res>_aggregate`,
  `<res>_by_pk`, the `insert`/`update`/`delete`-by-pk mutations, and the
  `<res>_bool_exp` / `<res>_order_by` / `<res>_insert_input` / `<res>_set_input`
  / `<res>_pk_columns_input` inputs + the free `<Model>Aggregate`) by composing
  the existing primitives. It **pins the snake_case wire names itself** — per
  root field, argument, generated input field, and `<Model>Aggregate` field name
  — so the resource is correct on a stock *camelCase* schema without a
  schema-wide `hasura_config()` (which `hasura_config()` stays an optional
  convenience for). Exposes `HasuraResource` (the assembled `query` / `mutation`
  / `types` bundle) and the `WriteBackend` protocol (the caller's
  authorized-write seam) (`resource.py`). The toy `tests/demo_schema.py` now
  builds its resource in this one call.
- **Grouping (`<res>_groups`) — NDC preview.** An optional grouped-aggregation
  root, enabled per resource with `hasura_resource(..., groupable=[...])`. Emits
  `<res>_groups(group_by, where, having, order_by, limit, offset): [<res>_group!]`
  where `<res>_group { key: <Model>GroupKey!, aggregate: <Model>Aggregate! }` —
  the typed group key paired with the **free** `<Model>Aggregate` (no reshape),
  composing `strawberry-django-aggregates`' public grouped surface
  (`shape_group_key` + `translate_group_by`/`translate_having`/`translate_order_by`
  + `shape_aggregate_row`). Shaped to the Hasura v3 / NDC `groups` semantics;
  **not** part of the stock `@refinedev/hasura` contract — preview (see
  `CONTRACT.md` "Grouping — NDC preview" and `ROADMAP.md`) (`grouping.py`).
- **JSON column filtering** — a `JSON_comparison_exp` (`_eq` / `_neq` /
  `_contains` / `_is_null`); `_contains` maps to Django `JSONField__contains`
  (`comparisons.py`, `filtering.py`).
- **Public-id foreign-key filters** — a `field_id_decode` hook decodes
  opaque-string (sqid) operands for non-`id` scalar columns (e.g. an FK exposed
  as a public id), threaded through the whole `where` walk including nested
  `_and` / `_or` / `_not` (`filtering.where_to_q`).
- **Write allowlists + operation toggles** — `hasura_resource(...)` takes
  `writable` / `insertable` / `updatable` column allowlists (fail-loud on unknown
  names) and `insert` / `update` / `delete` toggles to scope the mutation
  surface, plus a `get_aggregate_queryset` override for the aggregate/groups read
  source (`resource.py`).

### Changed

- Requires **`strawberry-django-aggregates >= 0.7.0`** — the release that adds
  the public `shape_group_key` / `translate_*` composition seam the grouping
  surface builds on.

## [0.1.0] — 2026-06-23

Initial release. A thin adapter that emits the GraphQL shape the stock
[`@refinedev/hasura`](https://refine.dev/docs/data/packages/hasura/) refine data
provider speaks, by composing `strawberry-django` and
`strawberry-django-aggregates` — the unmodified provider drives a
Strawberry/Django backend with no patching. See [`CONTRACT.md`](./CONTRACT.md)
for the target SDL and [`AGENTS.md`](./AGENTS.md) for the architecture.

### Added

- **Filtering** — `<resource>_bool_exp` + `<scalar>_comparison_exp` operator
  objects (`_eq`/`_neq`/`_gt`/`_in`/`_ilike`/`_is_null`/`_and`/`_or`/`_not`/…)
  translated to a Django `Q` (`comparisons.py`, `filtering.py`), with an
  optional `id_decode` hook for an opaque-string (sqid) `id` boundary.
- **Ordering** — `[<resource>_order_by!]` (per-field `order_by` enum) mapped onto
  `.order_by()` clauses (`ordering.py`).
- **Pagination** — bare `limit` / `offset` arguments → a queryset slice; an
  unordered page gets a deterministic `pk` tiebreaker so offset paging is stable
  (`connection.py`).
- **Aggregation (free)** — the `<resource>_aggregate { aggregate, nodes }`
  container whose `aggregate` field IS the native `<Model>Aggregate` type from
  `strawberry-django-aggregates` (`count`/`sum`/`avg`/`min`/`max`/…), composing
  `compute_aggregation` + `shape_aggregate_row` with **no reshape layer**
  (`aggregation.py`).
- **Mutations** — the `insert_<r>_one` / `update_<r>_by_pk` / `delete_<r>_by_pk`
  envelope translated to model kwargs via `input_to_dict` (`mutations.py`).
- **Snake-case wire naming** — `hasura_config()` / `SnakeNameConverter`, a
  `StrawberryConfig` flag keeping Python snake_case verbatim on the wire
  (`naming.py`).
- The `String`-typed pk-arg surface (`notes_by_pk(id: String!)`,
  `pk_columns.id`, `where.id._eq`) so refine's `idType: "String"` binds an opaque
  sqid as `$id: String!` unpatched.
- `py.typed` marker; the ORM boundary is type-checked with `mypy` +
  `django-stubs`.
- Runnable [`examples/`](./examples/) proof that the unmodified provider drives a
  schema built with this library, and an in-memory SQLite test suite covering
  every surface plus the emitted-SDL contract.

[0.3.1]: https://github.com/ang-ee/strawberry-django-hasura/releases/tag/v0.3.1
[0.2.0]: https://github.com/ang-ee/strawberry-django-hasura/releases/tag/v0.2.0
[0.1.0]: https://github.com/ang-ee/strawberry-django-hasura/releases/tag/v0.1.0
