# Changelog

All notable changes to `strawberry-django-hasura` are documented here. The
format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.10.0] — 2026-09-06

### Added

- Grouped row and exact-count roots accept a caller-owned
  `get_group_by_expressions(info, queryset, spec)` provider for selected
  forward to-one scalar or date axes. The adapter forwards the same
  request-bound expressions to the aggregate owner while retaining native validation,
  aliases, HAVING, ordering, and paging.

## [0.9.0] — 2026-09-05

### Added

- Explicit `sortable_aliases` map allowlisted wire fields to source-owned
  queryset annotations. Invalid or colliding declarations fail at schema
  construction; selected missing annotations fail before query execution.
  Native Django expressions and scoped querysets remain the consumer's owner.

### Fixed

- Explicit ORM list ordering appends the model primary key when absent,
  keeping ties stable across offset pages while preserving a selected PK
  direction. Source ordering without a wire order remains unchanged.

## [0.8.1] — 2026-09-05

### Fixed

- Lists, detail reads and aggregate nodes compose the active Strawberry Django
  optimizer extension before queryset evaluation. Consumer optimizer behavior,
  configured options and `DjangoOptimizerExtension.disabled()` now apply to
  these roots under both sync and async execution. Without an active extension,
  reads retain the native optimizer fallback. Public exports and SDL are
  unchanged.

## [0.8.0] — 2026-09-05

### Fixed

- Non-editable many-to-many fields are excluded from generated mutation inputs
  and rejected in explicit allowlists.
- To-many filters retain one parent row across lists, pagination, nodes,
  aggregate measures, groups, and exact group counts.
- ORM roots support sync and async execution through Strawberry Django's
  resolver boundary; row selections are optimized before safe evaluation.
- `__typename` selections no longer reach the aggregate measure compiler.
- Resource construction preserves caller-owned output types and isolates
  recursive input namespaces between independent schemas.
- Public-ID ordering supports custom primary keys; invalid and to-many sort
  paths fail at construction.
- Forward one-to-one writable inputs use their target scalar, file inputs use
  the upstream `Upload` mapping, and database-defaulted columns may be omitted.
- In-memory sources refresh scoped rows for each resolution rather than
  retaining them on potentially multi-operation contexts. Enum/UUID filtering,
  JSON structural comparisons, and empty boolean branches follow the declared
  comparison contract.

### Added

- Resource-local `json_paths`, `group_key_encoders`, and `filter_lookups`,
  composed with the native aggregate library's public configuration seams.
- `max_rows` caps list and aggregate-node responses while preserving exact
  aggregate totals; `max_groups` continues to cap only grouped pages.
- SQLite and PostgreSQL CI, async execution and tenant-isolation regressions,
  and an isolated installed-wheel smoke test gate publication.

### Changed

- Aggregate/group type prefixes default to the exact resource stem to avoid
  collisions when resources share a node. Pass `aggregate_name="Note"` to
  retain a legacy prefix, keeping custom prefixes unique within a schema.
- Caller-owned output fields must declare their snake_case names explicitly
  or use the schema's `hasura_config()` converter.
- Date and Time filters use their matching scalar comparison inputs.
- Explicit null comparison operands and negative pagination values raise;
  omit an operator or use `_is_null: true` / `false` for null tests.
- Requires the published `strawberry-django-aggregates >= 0.11.0` APIs.

## [0.7.1] — 2026-08-25

### Fixed

- **Direct to-many membership filters.** Restored the pre-0.7.0 acceptance of
  single-segment many-to-many and reverse-FK fields in `filterable`. Their
  comparison follows the related target key and the exact lookup is passed to
  Django unchanged, preserving membership semantics and `field_id_decode`.
  To-many relations remain forbidden anywhere in a multi-segment path,
  including as its terminal segment.

## [0.7.0] — 2026-08-25

### Added

- **Nested to-one filterable paths.** `hasura_resource(filterable=[...])` now
  accepts exact Django `__` paths whose non-terminal segments are to-one
  relations, such as `project__product`. The path is emitted as a scalar
  comparison field on the existing `<res>_bool_exp`
  (`project__product: ID_comparison_exp` for a default auto-pk terminal FK),
  matching the established direct-FK shape rather than introducing a second
  relation filtering model. Terminal FK group keys such as
  `project__product_id` can be passed directly to
  `where: {project__product: {_eq: ...}}` on the default raw-key boundary.
- **Named filter-path validation.** `FilterablePathError` is raised while the
  resource is built if a declared path crosses a to-many relation, contains a
  non-relation traversal segment, is malformed, or ends at an unknown/
  non-scalar attribute. The error is part of the public package API.

### Changed

- Direct scalar and direct relation filterables retain their existing input
  types and query behavior. Full nested paths may be keys in
  `field_id_decode` when their terminal relation uses a public id.

## [0.6.0] — 2026-07-15

### Added

- **Exact grouped cardinality root.** A groupable resource now emits
  `<res>_groups_count(group_by, where, having): Int!` alongside
  `<res>_groups`. It applies the same row filter, group translation, and
  post-aggregate `HAVING` predicate while deliberately ignoring the row
  root's order, limit, and offset. Counting delegates to the public
  database-side `AggregateBuilder.count_groups(...)` seam.
- **Grouped-count metadata.** `HasuraResource.groups_count_root` exposes the
  generated root name for downstream schema metadata and code generators.

### Changed

- Requires `strawberry-django-aggregates >= 0.10.0` for the public exact-count
  composition seam.

## [0.5.0] — 2026-07-05

### Added

- **Nested object insert for declared to-many relations.** `hasura_resource`
  gains a `nested=[NestedInsert(relation="lines", model=LineModel)]` knob that
  exposes the Hasura array-relationship insert shape —
  `insert_<res>_one(object: {..., lines: {data: [<child>...]}})`. Each declared
  relation contributes a child row input (`<res>_<relation>_insert_input`) built
  from the child model's editable columns and its `{data: [...]}` envelope
  (`<res>_<relation>_arr_rel_insert_input`), plus an optional field on the
  parent `<res>_insert_input`. The child's foreign key back to the parent is
  excluded (the nesting supplies it), and the child input carries an **optional
  public `id`** with every column optional, so the one input drives both the
  nested insert (`id` omitted → a new child) and a consumer's authored
  upsert/diff `_save` operation (`id` present → an existing child). The built
  `HasuraResource` exposes `nested_inserts`, `nested_input_types`, and
  `nested_arr_input_types` so a consumer can reuse the child input for its own
  mutation. `NestedInsert` is a new public export.
- **`NestedInsert` is declared, validated, hashable.** `relation` accepts the
  `related_name`, the default `<child>_set` accessor, or the related query
  name, and must resolve to a reverse to-many FK whose target is `model` — an
  unknown name, a forward field, a reverse one-to-one/many-to-many, a
  mismatched `model`, or the back-FK listed in `insertable` fails at build,
  not at the first request. `nested=` requires `insert=True` (a read-only
  resource never emits write-shaped child inputs) and a child column literally
  named `id` fails fast instead of silently shadowing the injected upsert key.
  The frozen spec freezes its sequence knobs to tuples, so it stays hashable.
  `public_id_columns=[…]` types the named child columns as `ID` (decoding
  stays the write backend's concern, exactly like the parent's write path) and
  `id_column=…` excludes the child's own public-id column from the writable
  set, mirroring the top-level knob.
- **`input_to_dict` recurses through nested input objects.** It now reduces the
  `{data: [...]}` envelope (and any nested input) to plain nested dicts so the
  caller's `write_backend` receives ready-to-persist kwargs, never a
  half-decoded strawberry input instance. Only declared strawberry **inputs**
  reduce: a scalar list (an m2m `[ID!]` array), a tuple, and a custom-scalar
  value that is itself a dataclass (a `Money` object) pass through verbatim.
  An explicit `<relation>: null` envelope reaches the backend with the key
  absent (Hasura: a null relationship envelope means no children, not a null
  column). Persistence and atomicity stay the `write_backend`'s concern — the
  library owns the input shape only.

### Fixed

- **Reverse relation accessors are never client-writable.** The writable-field
  scan names reverse accessors (`ManyToOneRel` / `ManyToManyRel` /
  `OneToOneRel`) as not-writable instead of admitting reverse many-to-many
  accessors: a model that is the *target* of another model's
  `ManyToManyField` no longer risks exposing that reverse relation as a
  client-settable array — and building an insert surface over such a model no
  longer crashes (`'ManyToManyRel' object has no attribute 'has_default'`).

- **`Decimal_comparison_exp` — exact fixed-point filtering.** A `DecimalField`
  column now filters through a new `DecimalComparison` (`comparisons.py`) whose
  operands are strawberry `Decimal` scalars (exact strings on the wire), across
  the full numeric operator set (`_eq`/`_neq`/`_gt`/`_gte`/`_lt`/`_lte`/`_in`/
  `_nin`/`_is_null`). Both the model path and the `hasura_run_query_resource`
  path pick it up through the shared `inputs.COMPARISON_FOR_TYPE` owner.

### Changed

- **`decimal.Decimal` maps to `DecimalComparison`, not `FloatComparison`**
  (`inputs.COMPARISON_FOR_TYPE`). A high-precision money/quantity value no
  longer round-trips through a lossy double on the filter surface. The
  aggregate surface was already exact — `strawberry-django-aggregates` types
  `sum`/`avg`/`min`/`max` over a `DecimalField` as `Decimal` (only the
  statistical `stddev`/`variance` remain `Float`, as their result is). Emitted
  SDL changes for any schema with a `DecimalField` column; `CONTRACT.md` and
  the SDL-marker tests are updated to match.

## [0.4.0] — 2026-07-02

### Added

- **`HasuraResource` now exposes role-named generated types and root names**
  so consumers can read the built surface directly instead of re-templating the
  Hasura naming convention. Model resources expose `node_type`,
  `filter_type`, `order_by_type`, `insert_input_type`, `set_input_type`,
  `pk_columns_input_type`, `aggregate_container_type`, `aggregate_type`,
  `group_type`, `group_key_type`, `group_by_spec_type`, `group_order_type`,
  `having_type`, all root field names, and the builder-decided write facts
  (`enabled_operations`, `insertable_fields`, `updatable_fields`). The
  read-only `hasura_run_query_resource(...)` path exposes the matching read
  roles. `aggregate_container_type` is the Hasura `<res>_aggregate` wrapper;
  `aggregate_type` remains the inner `aggregate` payload (`<Model>Aggregate` on
  the model path, count-only `<Node>Aggregate` on the row-source path).

## [0.3.2] — 2026-06-30

### Fixed

- **The read resolvers now lean on strawberry-django's
  `DjangoOptimizerExtension`** instead of N+1-ing nested relations. The list and
  `<res>_aggregate { nodes }` resolvers returned `list(...)`, which evaluated the
  queryset before the optimizer's `_result_cache is None` gate — so a consumer
  with the extension installed still saw nested loads scale with the row count.
  They now return the lazy queryset and the extension collapses nested relations
  to a constant query count (`select_related` + `prefetch_related` + `.only()`).
- **`<res>_by_pk` composes `optimize()`** so the single row's nested selections
  are optimized too. `.first()` evaluates eagerly (the extension only
  auto-optimizes a returned queryset), so a `by_pk { author { … } }` selection
  was a separate SELECT per to-one relation; it is now folded into the row's own
  JOIN. `optimize()` is composed, not reimplemented — and works with or without
  the extension installed.

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

[0.8.1]: https://github.com/ang-ee/strawberry-django-hasura/releases/tag/v0.8.1
[0.8.0]: https://github.com/ang-ee/strawberry-django-hasura/releases/tag/v0.8.0
[0.7.1]: https://github.com/ang-ee/strawberry-django-hasura/releases/tag/v0.7.1
[0.7.0]: https://github.com/ang-ee/strawberry-django-hasura/releases/tag/v0.7.0
[0.6.0]: https://github.com/ang-ee/strawberry-django-hasura/releases/tag/v0.6.0
[0.5.0]: https://github.com/ang-ee/strawberry-django-hasura/releases/tag/v0.5.0
[0.4.0]: https://github.com/ang-ee/strawberry-django-hasura/releases/tag/v0.4.0
[0.3.2]: https://github.com/ang-ee/strawberry-django-hasura/releases/tag/v0.3.2
[0.3.1]: https://github.com/ang-ee/strawberry-django-hasura/releases/tag/v0.3.1
[0.2.0]: https://github.com/ang-ee/strawberry-django-hasura/releases/tag/v0.2.0
[0.1.0]: https://github.com/ang-ee/strawberry-django-hasura/releases/tag/v0.1.0
