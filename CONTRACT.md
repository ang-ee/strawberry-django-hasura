# Target SDL contract — stock `@refinedev/hasura`

What the **stock** refine `@refinedev/hasura` data provider (v7.0.1,
`namingConvention: "hasura-default"`) expects from the GraphQL schema. Extracted
from refine source `packages/hasura/src` (2026-06-23) and the proven A/B spike.
The `strawberry-django-hasura` adapter must emit exactly this shape per model so
the provider needs **no patching**. Running example: model `Note`, refine
resource `notes`, singular field stem `notes`.

The wire convention is **snake_case** (Hasura-default) — install
`hasura_config()` (a `StrawberryConfig` name converter) on the schema so every
column / argument name is verbatim, not camelCased.

## Queries

- **List** —
  `notes(where: notes_bool_exp, order_by: [notes_order_by!], limit: Int, offset: Int): [Note!]!`
  - provider reads `data.notes` directly (the rows); the list total rides the
    aggregate below (`data.notes_aggregate.aggregate.count`).
- **By-pk** — `notes_by_pk(id: String!): Note`
- **Aggregate** — `notes_aggregate(where: notes_bool_exp): notes_aggregate!`
  - `type notes_aggregate { aggregate: NoteAggregate!  nodes: [Note!]! }`
  - `aggregate` is the **native** `<Model>Aggregate` from
    `strawberry-django-aggregates` — `{ count: Int!, sum { <field> },
    avg { <field> }, min { <field> }, max { <field> }, … }`. **No reshape.**

## Mutations (provider derives these operation names)

- `insert_notes_one(object: notes_insert_input!): Note!`
- `update_notes_by_pk(pk_columns: notes_pk_columns_input!, _set: notes_set_input!): Note!`
- `delete_notes_by_pk(id: String!): Note`

## Filter — `notes_bool_exp`

Per filterable field a `<scalar>_comparison_exp` object, plus boolean
composition:

- comparators: `_eq, _neq, _gt, _gte, _lt, _lte, _in, _nin, _is_null`
- string: `_like, _nlike, _ilike, _nilike` (+ Postgres-only `_iregex`,
  `_similar`, `_nsimilar` accepted in the SDL)
- JSON: `_contains` for object/list containment, plus equality/null operators
- composition: `_and: [notes_bool_exp!]`, `_or: [notes_bool_exp!]`,
  `_not: notes_bool_exp`

refine's `hasuraFilterOperatorMappings` sends `eq→_eq`, `ne→_neq`,
`lt/gt/lte/gte`, `in→_in`, `nin→_nin`, `contains→_ilike`, `containss→_like`,
`null/nnull→_is_null` (+ Postgres regex/similar for `startswith`/`endswith`).
Maps to Django `Q`: `_eq→exact`, `_neq→~exact`, `_in→in`, `_nin→~in`,
`_like`/`_ilike` accept Hasura SQL-LIKE patterns and map common
leading/trailing `%` forms to portable `contains`/`startswith`/`endswith`
lookups (`contains` from stock refine arrives as `_ilike: "%term%"`),
`_gt→gt`, …, `_is_null:true→isnull`; JSON `_contains` maps to Django
`JSONField__contains`. A raw `_like`/`_ilike` value without `%` is also treated
as a substring shorthand for authored callers.

The portable operators are mapped in the default `filtering._LOOKUPS`; the
Postgres-only `_iregex`/`_similar`/`_nsimilar` are accepted in the SDL but
**not** in the portable default map. Sending one on a backend that has not
registered it **raises** (it is never silently dropped — a silently-ignored
filter would widen a permission-naive read). A Postgres project registers the
lookup in its own `_LOOKUPS`.

## order_by — `notes_order_by`

- `input notes_order_by { <field>: order_by }` — a per-field input of the
  `order_by` enum (a client may pass `[{ word_count: desc }, { title: asc }]`).
- `enum order_by { asc desc }`
- Maps to Django `.order_by()` (`desc` → a `-` prefix).

## Paging

- bare `limit: Int` / `offset: Int` args → queryset slice. An unordered page
  gets a deterministic `pk` tiebreaker; a caller-supplied `order_by` must be
  *total* to page deterministically over it.

## sqid / idType boundary

- The public `id` field on `Note` is the **sqid** (the DB pk is hidden); the
  output type stays `id: ID!` (`ID` serializes a string fine).
- Every **pk-arg surface** — `notes_by_pk(id:)`, `notes_pk_columns_input.id`,
  `notes_bool_exp.id._eq` — is typed GraphQL **`String`**, NOT `ID`. refine's
  `getIdType(resource, idType)` returns the configured `idType` verbatim and
  declares the id variable `$id: <idType>!`; its `idType` enum is
  `uuid | Int | String | Numeric` (no `ID`), and the **default is `uuid`**. A
  sqid project therefore MUST construct the provider with
  `dataProvider(client, { idType: "String" })` so the opaque sqid binds as
  `$id: String!` (an `ID` arg would reject a `String!` variable).
- Decoding the sqid to the pk is the consumer's concern: pass an `id_decode`
  hook to `where_to_q` and decode at the resolver boundary (see
  `examples/demo_schema.py`).

## Boundary notes

- The provider also honors `meta.gqlQuery` to override the document (the
  aggregate rides this custom path); the default path builds via
  `gql-query-builder` from `meta.fields`.
- Resource name → the list/aggregate/by-pk field stems and the
  insert/update/delete mutation names above are all keyed off the **plural**
  resource (`notes`, `notes_aggregate`, `insert_notes_one`, …).
- **Empty boolean operands** (`_or: []`, `_not: {}`) follow Django `Q` algebra
  — an empty expression is a no-op (matches every row in the already-scoped
  queryset), not Hasura's "matches none". The stock provider never emits these;
  a hand-written `meta.gqlQuery` that relies on the empty-operand edge should
  not assume Hasura semantics. Row scoping remains the consumer's `base_qs()`
  concern regardless (this library is permission-naive).

## Non-model resources (`hasura_run_query_resource`)

A resource whose rows are computed/foreign (no Django table) is built with
`hasura_run_query_resource(node, *, name, filterable, sortable, source)`. It
emits the **same** stock surface as a model resource — the `<res>` list (with
`where` / `order_by` / `limit` / `offset`), `<res>_by_pk(id)`, and
`<res>_aggregate { aggregate { count } nodes }` — with two differences:

- **Read-only.** No `insert`/`update`/`delete` roots (an empty mutation holder
  that merges to nothing).
- **Count-only aggregate.** `<Node>Aggregate` carries only `count: Int!` (a
  computed source needs the row total for pagination, not the SQL aggregate
  compiler).

`source` is a `RowSource` — `query(info, *, where, order_by, limit, offset)` and
`count(info, *, where)` — the pushdown seam. The default `InMemoryRowSource`
evaluates the `<res>_bool_exp` / `order_by` / paging in Python via
`where_matches` / `apply_in_memory` (the in-memory sibling of `where_to_q`); a
transport-backed source pushes the predicate to its owner. The same `_bool_exp`
operator set and fail-fast-on-unmapped-operator stance as the model path apply.

In-memory NULL semantics follow the model path's default SQLite backend: NULLs
sort **first on `asc`, last on `desc`**; a positive `_like`/`_ilike` does **not**
match a NULL row (the negated family does, like Django's `~Q`); and an explicit
`null` operand (e.g. `_gt: null`) carries no constraint — use `_is_null`.

## Grouping — NDC preview (NOT stock `@refinedev/hasura`)

`<res>_groups` is a **preview** surface emitted **only** when the resource is
built with `groupable=[...]`. It is *not* part of the stock `@refinedev/hasura`
contract above — that provider never sends `group_by`. It is shaped to the
Hasura v3 / NDC (Native Data Connector) `groups` semantics so a custom client
(or a future DDN-compatible provider) can drive grouped analytics:

```graphql
notes_groups(
  group_by: [NoteGroupBySpec!]!   # dimensions: { field, granularity }
  where:    notes_bool_exp         # pre-group filter (the outer predicate)
  having:   NoteHaving             # predicate over AGGREGATES only
  order_by: [NoteGroupOrder!]      # by a dimension alias or an aggregate
  limit: Int  offset: Int          # offset paging
): [notes_group!]!

type notes_group {
  key:       NoteGroupKey!   # typed composite key — one field per dimension,
                             #   choices→enum, date buckets + `_range` siblings
  aggregate: NoteAggregate!  # the SAME free aggregate type — no reshape
}
```

- **Composed, not forked.** The whole surface composes
  `strawberry-django-aggregates` through its public API only: one
  `AggregateBuilder` emits `NoteGroupKey` / `NoteGroupBySpec` / `NoteHaving` /
  `NoteGroupOrder` (and the free `NoteAggregate`); `translate_group_by` /
  `translate_having` / `translate_order_by` parse the wire inputs; and the row
  shapers `shape_group_key` + `shape_aggregate_row` fill `{ key, aggregate }`.
  The `aggregate` field IS the free `NoteAggregate` — the aggregate stays free.
- Generated wire names are snake_case (`NoteGroupKey` columns; `NoteHaving`
  operators like `count_gt` / `sum_<field>_gt`).
- **Granularity** uses the aggregates `Granularity` enum (TIME `date_trunc` +
  NUMBER `date_part` tracks). NDC models granularity as connector-declared
  `extraction` functions; aligning to that naming is a forward step for when
  DDN ships its GraphQL `group_by`.
- Offset paging is **non-deterministic without `order_by`** (group rows have no
  intrinsic order) — pass `order_by` for stable pages; build the resource with
  `hasura_resource(max_groups=…)` to cap an unbounded high-cardinality grouping
  (default uncapped). Reads run on the caller's scoped queryset
  (permission-naive), with the Hasura `where` applied before grouping.
- **Preview:** the DDN GraphQL `group_by` SDL is unpublished (Hasura
  `graphql-engine#10786`), so these field/argument names may change to track it.
  The stock list / aggregate / CRUD SDL above is unaffected (grouping is purely
  additive).
