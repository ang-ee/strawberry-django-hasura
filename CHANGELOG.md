# Changelog

All notable changes to `strawberry-django-hasura` are documented here. The
format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

[0.1.0]: https://github.com/ang-ee/strawberry-django-hasura/releases/tag/v0.1.0
