# AGENTS.md

Agent / contributor entry point for `strawberry-django-hasura`. The human entry
point is [`README.md`](./README.md); the wire contract is
[`CONTRACT.md`](./CONTRACT.md).

> This library is a **thin adapter**. Its whole job is to emit the GraphQL shape
> the stock `@refinedev/hasura` refine data provider speaks, by composing
> `strawberry-django` and `strawberry-django-aggregates`. If a change makes it
> *more* than thin glue, stop and reconsider.

## Architecture — the five surfaces

The package is model-independent primitives plus per-model type declarations the
consumer writes. The owner of each concern is an existing library; this adapter
only translates the Hasura vocabulary onto it. The wire convention is
**snake_case** (Hasura-default), so the schema installs `hasura_config()` (a
`StrawberryConfig` name converter) — a config flag, never a fork.

| Surface | Module(s) | Owner it composes | Translates |
| --- | --- | --- | --- |
| Filtering | `comparisons.py`, `filtering.py` | Django ORM (`Q`) | `<resource>_bool_exp` + `<scalar>_comparison_exp` (`_eq`/`_neq`/`_ilike`/`_in`/`_and`/`_or`/`_not`/…) → a `Q` |
| Ordering | `ordering.py` | Django `.order_by()` | `[<resource>_order_by!]` (per-field `order_by` enum) → ordering clauses |
| Pagination | `connection.py` | queryset slicing | bare `limit` / `offset` args → a queryset slice |
| Aggregation | `aggregation.py` | `strawberry-django-aggregates` (`AggregateBuilder` + `compute_aggregation` + `shape_aggregate_row`) | the **free** `<resource>_aggregate { aggregate, nodes }` — `aggregate` IS the native `<Model>Aggregate` type, zero reshape |
| Mutations | `mutations.py` | the Django model | the `insert_<r>_one` / `update_<r>_by_pk` / `delete_<r>_by_pk` envelope → model kwargs |

`naming.py` holds the snake_case `NameConverter` + `hasura_config()` (the
model-independent wire-naming flag). `connection.py` also owns the
`<resource>_aggregate` container shell (paging and the aggregate ship together —
the list total rides `aggregate.count`). `__init__.py` re-exports the public API
— **that export list is the contract; keep it stable.**

`resource.py` is the one-call **builder** on top of the surfaces:
`hasura_resource(node, *, model, name, filterable, sortable, aggregatable,
get_queryset, write_backend, id_decode)` assembles the whole resource (the
inputs, the query/mutation roots, and the free aggregate) by composing the
primitives above — it owns only *composition + naming*. It derives the
comparison/order scalar of each column from the **Django field** and the
`insert`/`_set` writable columns from the model's editable, non-pk, non-auto
fields; row scoping (`get_queryset`), authorized writes (`write_backend`, a
`Protocol`), and the sqid⇄pk boundary (`id_decode`) stay caller-supplied (no
rebac/Angee imports leak in). Crucially it **pins the snake_case wire names
itself** — per root field, argument, generated input field, and
`<Model>Aggregate` field name — so the resource is correct on a stock
*camelCase* consuming schema (e.g. Angee) with no schema-wide `hasura_config()`.
The generated `<res>_bool_exp` references itself (`_and`/`_or`/`_not`), so the
generated types are hosted in a per-resource synthetic module for forward-ref
resolution at schema build.

## The aggregate is FREE — wire, don't reshape

This is the headline of the Hasura dialect. The native `<Model>Aggregate` type
that `strawberry-django-aggregates`' `AggregateBuilder` emits — `{ count: Int!,
sum { <field> }, avg { <field> }, min { <field> }, max { <field> }, … }` — *is*
Hasura's `aggregate { … }`. So `aggregation.py` carries **no flat→nested reshape
layer**:

- `build_aggregate_type(model)` returns `AggregateBuilder(...).build()
  .aggregate_type` — the library's own type, unchanged.
- `make_aggregate_resolver` composes the two *public* primitives —
  `compute_aggregation` (runs the one query) + `shape_aggregate_row` (fills the
  type) — deriving the requested `(op, field)` pairs from the GraphQL selection.

Contrast the nestjs path, whose `aggregation.py` needed ~300 LOC
(`build_aggregate_types` + `make_aggregate_resolver` + `_row_to_response`) to
fold flat composite-key rows into a `NoteAggregateResponse { groupBy, count{},
sum{} }` envelope. The Hasura shape eliminates that whole file's worth of glue.

## The ownership rule — compose, never fork

The underlying libraries are **composed, never modified**:

- `strawberry` / `strawberry-django` own type generation and the ORM seam. We do
  not patch them — we declare Hasura-shaped inputs and map them onto the ORM.
- `strawberry-django-aggregates` owns the aggregate TYPE *and* its execution
  (`AggregateBuilder`, `compute_aggregation`, `shape_aggregate_row`).
  `aggregation.py` only walks the selection (the Hasura operator vocabulary it
  owns) and filters via the adapter's own `where_to_q`.

A capability gap upstream is the signal — file it / fix it upstream, then
compose the result. Never work around it here.

## How to add or extend a surface

1. **Find the owner first.** Every Hasura operator maps to an existing ORM
   lookup, an `.order_by()` clause, a slice, a model write, or an `AggregateOp`.
   Name that owner before writing code.
2. **New filter operator** → add the field to the relevant `*Comparison` in
   `comparisons.py` (with the Hasura wire name via `strawberry.field(name="_x")`)
   and one row in `filtering._LOOKUPS` mapping it to a Django lookup suffix.
   `comparison_to_q` reads it by `getattr`, so the addition is declarative. Keep
   `_LOOKUPS` portable — Postgres-only operators (`_iregex`, `_similar`) belong
   in a project's own map, not the shared default.
3. **New aggregate measure** → it is already free if the op exists on the native
   `<Model>Aggregate` (it does for `count`/`sum`/`avg`/`min`/`max`/`stddev`/…).
   If an op is missing, add it to `strawberry-django-aggregates`, not here.
4. **Keep callers thin.** Resolvers (in the consumer's schema) declare intent and
   dispatch to these helpers; they don't accumulate filter/sort/persistence
   rules.
5. **Update the contract.** If a change alters the emitted SDL, update
   `CONTRACT.md` and the SDL-marker assertions in `tests/test_sdl_contract.py` in
   the same change.

## Gotchas

- **`id` ⇄ `sqid` and `idType: "String"`.** The public `id` field stays
  `ID` on output (an `ID` serializes a string fine), but **every pk-arg surface**
  — `notes_by_pk(id:)`, `notes_pk_columns_input.id`, `notes_bool_exp.id._eq` — is
  typed GraphQL **`String`**, never `ID`. refine's `getIdType` declares the id
  variable `$id: <idType>!` verbatim and its `idType` enum is
  `uuid | Int | String | Numeric` (default **`uuid`**). A sqid (or any opaque
  string id) project MUST build the provider with
  `dataProvider(client, { idType: "String" })` — an `ID` arg would reject the
  `String!` variable. Decoding the sqid to the pk is the consumer's concern: pass
  an `id_decode` hook to `where_to_q` and decode at the resolver boundary. This
  adapter never inspects a value to guess whether it is a sqid.
- **Build the aggregate type AFTER `django.setup()`.** `AggregateBuilder.build()`
  introspects the model's relation tree (`model._meta.get_fields()`), which is
  only ready once the app registry is fully populated. Build it in your schema
  module (imported after setup), not during `app.models` import — the tests
  split `tests/models.py` (the model) from `tests/demo_schema.py` (the schema)
  for exactly this reason, which also mirrors a real consumer.
- **`SUM` over an integer field is `BigInt`, not `Int`.** The native aggregate
  types the SUM of an `IntegerField` as `BigInt` (a JSON string on the wire) so a
  64-bit Postgres `bigint` survives JavaScript's `Number.MAX_SAFE_INTEGER`. A
  client / test reads it via `Number(...)` / `int(...)`. This is the library's
  type, unchanged — proof the aggregate is free.
- **Reads must pass an already-scoped queryset.** This library is
  permission-naive (same stance as `strawberry-django-aggregates`): it trusts the
  queryset it is handed. Apply row-level (REBAC / `accessible_by` /
  `filter(owner=...)`) scoping in your `base_qs()` before the adapter filters,
  orders, paginates, or aggregates over it. Never add an `actor`/`user`
  parameter to these helpers.
- **`_set` is a leading-underscore wire name.** Hasura's update mutation takes
  `_set:`. Name the resolver parameter `_set` directly; the snake-name converter
  keeps it verbatim (it does not derive `_set` from any camelCase identifier).
- **Snake-case naming is a `StrawberryConfig`, not a patch.** Pass
  `config=hasura_config()` to `strawberry.Schema(...)`. Without it Strawberry
  camelCases `word_count` → `wordCount` and the provider's `hasura-default`
  document won't match.

## Running the checks

The quality gate mirrors the sibling `strawberry-django-aggregates`:

```sh
uv sync --group dev                       # install workspace + dev tools
uv run pytest                             # all surfaces, in-memory SQLite
uv run ruff check .                       # lint (line length 79)
uv run ruff format --check .              # format
uv run mypy strawberry_django_hasura      # type-check the package
```

Tests configure Django programmatically in `tests/conftest.py` (no project
needed); the toy `Note` schema (`tests/demo_schema.py`) is the fixture and reads
like `CONTRACT.md`. `tests/models.py` holds the model so Django registers it and
pytest-django builds its table.

The Node proof that the **stock** provider drives the schema lives in
[`examples/`](./examples/) and is not part of `pytest` — see its README.

## Definition of done

- `pytest`, `ruff check`, `ruff format --check`, and
  `mypy strawberry_django_hasura` are all green.
- The public API (`__init__` exports) is unchanged unless the change is
  intentional and the `CONTRACT.md` + SDL tests are updated to match.
- No underlying library was forked or patched; any new capability was composed
  (or added upstream, then composed).
- The aggregate stays free — no flat→nested reshape layer creeps back in.
- Emitted SDL still matches `CONTRACT.md`.
