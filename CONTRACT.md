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

The builders pin their generated roots, arguments, inputs, and aggregate types.
Consumer-owned output types are preserved; on a camelCase schema, explicitly
name snake_case output fields. The examples below use `aggregate_name="Note"`
to retain the legacy type prefix. Since 0.8, a builder's default prefix is the
exact resource stem (`notesAggregate`, `notesGroupKey`, etc.), isolating two
resources that share a node but expose different measures. Both builders accept
the optional `aggregate_name` override; an override must be unique in a schema.

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
    `sum`/`avg`/`min`/`max` over a `DecimalField` stay exact `Decimal` (the
    aggregates library types them straight from the column) — only the
    statistical `stddev`/`variance` are `Float`, as their result is.

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
- decimal: a `DecimalField` column takes the exact `Decimal_comparison_exp`
  (the full numeric operator set above, with strawberry `Decimal` operands —
  exact strings on the wire), **not** `Float_comparison_exp`; a
  high-precision money/quantity value round-trips without a lossy double
- JSON: `_contains` for object/list containment, plus equality/null operators
- date / time: `Date_comparison_exp` and `Time_comparison_exp` preserve the
  output scalar (`Date` / `Time`) for equality, ordering, and membership
- explicit null comparison operands raise, including `_is_null: null`; omit
  an operator to leave it unconstrained or use `_is_null: true` / `false`
- composition: `_and: [notes_bool_exp!]`, `_or: [notes_bool_exp!]`,
  `_not: notes_bool_exp`

The adapter's direct-relation convention is a scalar comparison on the related
key: a direct FK such as `project` emits `project: ID_comparison_exp` for
Django's default auto pk, whose operands are GraphQL `String`. A direct
single-segment to-many field keeps the same convention. For example,
`filterable=["groups"]` on a many-to-many relation emits
`groups: ID_comparison_exp` and applies `_eq` as Django `Q(groups=<operand>)`,
matching rows that contain that related value. A `field_id_decode["groups"]`
decoder, when supplied, is applied before the same membership lookup.
The resource composes a parent-key subquery for to-many predicates so matching
multiple related values still yields one parent row. Lists, aggregate nodes,
measures, groups, and group counts all preserve that parent cardinality.

Model resources may also declare a filterable Django path through to-one
relations, for example `filterable=["project__product"]`. The nested path is
another scalar comparison in the same `<res>_bool_exp`, under its exact
collision-free Django lookup name:

```graphql
input tasks_bool_exp {
  project: ID_comparison_exp
  project__product: ID_comparison_exp
  # ... _and / _or / _not
}
```

The filter value is applied as the matching Django lookup
`project__product=<operand>`. Every non-terminal segment must be a to-one
relation (FK / one-to-one), and a to-many relation anywhere in a multi-segment
path—including its terminal segment—raises `FilterablePathError` while the
resource is built. An unknown/non-field terminal is rejected likewise. A
terminal relation uses its target field's scalar type, including a non-ID
`to_field`. A path may also end at a scalar column (`project__title` compares
the CharField with `String_comparison_exp`), and a terminal reverse one-to-one
resolves to the related model's primary key. A full path
listed in `field_id_decode` uses the same `ID_comparison_exp` while applying its
caller-supplied public-id decoder, exactly like a direct public-id FK.

No nested bool-exp input types are generated: the one deterministic type is
still `<res>_bool_exp`, and exact `__` paths cannot collide because Django
forbids `__` inside a model field name. For a terminal FK grouping emits the
same path with the terminal attname suffix (for example
`project__product_id`); its raw key value can be passed directly to
`where: {project__product: {_eq: <group key>}}` when the path uses the default
raw-key boundary. If the path is listed in `field_id_decode` for an opaque
public id, the caller must encode that model-native group key before filtering;
group shaping belongs to the aggregates library and does not apply the
adapter's filter decoder.

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
- The public `id` ordering column maps to `id_column`, including custom primary
  keys. Unknown paths and to-many ordering paths fail at resource construction.

## Paging

- bare `limit: Int` / `offset: Int` args → queryset slice. An unordered page
  gets a deterministic `pk` tiebreaker; a caller-supplied `order_by` must be
  *total* to page deterministically over it.
- Negative limits and offsets raise consistently for ORM and row sources.
  Optional `max_rows` caps lists and aggregate nodes; optional `max_groups`
  caps group rows. Both default to `None`. Counts and aggregate math always
  use the complete scoped, filtered source rather than the capped page.

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

In-memory NULL ordering follows the model path's default SQLite backend: NULLs
sort **first on `asc`, last on `desc`**; a positive `_like`/`_ilike` does **not**
match a NULL row (the negated family does, like Django's `~Q`). An explicit
`null` comparison operand raises in both paths — use `_is_null`. A null
`where` or null column comparison object still represents an absent filter.

Enum rows compare and sort by their stringified underlying values. JSON
equality is structural and distinguishes booleans from numbers; `_contains`
uses recursive object/array containment, with missing keys distinct from null.
Plain JSON strings use equality for containment rather than substring matching.
Date and Time inputs round-trip their corresponding scalar values.

`InMemoryRowSource` does not cache rows on the request context: contexts can
span operations, particularly over WebSockets. It materializes `get_rows` for
each query/count resolution. A caller can memoize inside an explicitly
operation-scoped source. `max_rows` and `aggregate_name` are supported by this
builder as well.

## Write and execution boundaries

Non-editable fields, including forward M2M relations, are excluded from
generated writes and rejected when explicitly allowlisted. Forward one-to-one
inputs use the target scalar like FKs. File and image inputs use the native
Strawberry Django `Upload` mapping; transport configuration, authorization,
and file validation remain consumer concerns. Columns with `db_default` can
be omitted from inserts so Django applies the database default.

ORM roots compose Strawberry Django's resolver and optimizer facilities for
sync and async execution. Sources supplied to the builder define root row
scope; the builder does not apply a second node-level `get_queryset` policy
after pagination. Backend authorization, validation, relation-ID checks and
transactionality remain in the caller's `WriteBackend`. GraphQL meta fields
such as `__typename` are never forwarded as ORM aggregate measures.

## Nested object insert (opt-in, additive)

A resource built with `nested=[NestedInsert(relation="lines", model=Line)]`
exposes Hasura's array-relationship insert on the existing insert root — the
stock provider never sends it, so this is purely additive to the CRUD SDL
above:

```graphql
insert_<res>_one(object: {
  ...                         # the parent's own <res>_insert_input columns
  lines: { data: [ <res>_lines_insert_input! ]! }   # optional envelope
}): <Node>!

input <res>_lines_arr_rel_insert_input { data: [<res>_lines_insert_input!]! }
input <res>_lines_insert_input {
  id: String            # optional — the upsert key (omit to insert a new child)
  <child columns...>    # every column optional; the FK back to the parent is
                        #   supplied by the nesting and never appears here
}
```

- **Shape only.** The library generates the child input, its `{data: […]}`
  envelope, and the optional parent field, and reduces the whole envelope to
  plain nested dicts via `input_to_dict`
  (`{"lines": {"data": [{…}, …]}}`). **Persistence and atomicity are the
  `write_backend`'s concern** — it writes the parent, then each child under the
  parent FK, in one transaction, rolling back on a child failure. The library
  adds no write path (same permission-naive stance as the flat CRUD surface).
- **One input, two callers.** The child input carries an optional public `id`
  and all-optional columns so it drives both the nested insert (`id` omitted)
  and a consumer's authored upsert/diff `_save` mutation (`id` present addresses
  an existing child). The built `HasuraResource` exposes `nested_input_types` /
  `nested_arr_input_types` for that reuse.
- **Child stem** defaults to `<res>_<relation>`; `NestedInsert(name=…)`
  overrides it. `NestedInsert(public_id_columns=[…])` types the named child
  columns as `ID` (public ids) — decoding them stays the write backend's
  concern, exactly like the parent's write path. `NestedInsert(id_column=…)`
  excludes the child's own public-id column from the writable set
  (server-owned), mirroring the top-level `id_column`.
- **Declared, validated.** `relation` accepts the `related_name`, the default
  `<child>_set` accessor, or the related query name, and must resolve to a
  reverse to-many FK whose target is `model` — an unknown name, a forward
  field, a reverse one-to-one/many-to-many, a mismatched `model`, or the
  back-FK listed in `insertable` fails at build, not at the first request.
  `nested=` requires `insert=True`, so a read-only resource never emits
  write-shaped child input types. An explicit `<relation>: null` envelope
  reaches the backend with the key absent (Hasura: a null relationship
  envelope means no children, not a null column).

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

notes_groups_count(
  group_by: [NoteGroupBySpec!]!   # same dimensions as notes_groups
  where:    notes_bool_exp         # same pre-group filter
  having:   NoteHaving             # same post-aggregate predicate
): Int!                            # exact total before limit / offset

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
- **Exact cardinality.** `<res>_groups_count` shares `group_by`, `where`, and
  `having` semantics with `<res>_groups`, but deliberately has no ordering,
  limit, or offset. It delegates to the aggregation owner's database-side
  count path and never materializes every group row in Python.
- **Preview:** the DDN GraphQL `group_by` SDL is unpublished (Hasura
  `graphql-engine#10786`), so these field/argument names may change to track it.
  The stock list / aggregate / CRUD SDL above is unaffected (grouping is purely
  additive).


## Resource-local aggregate and filtering configuration

The hasura_resource builder accepts three optional mappings. They are copied
at resource construction; building another resource or mutating the caller's
dictionaries does not change an existing schema.

- json_paths={"metadata.region": "str", "metadata.amount": "int"} declares
  the aggregate owner's typed JSON allowlist. Include paths in groupable or
  aggregatable to expose them. Emitted fields retain canonical aliases,
  for example metadata__amount. List totals, grouped execution, having,
  group ordering and exact group counts share the same allowlist. Selection
  aliases are translated only for declared JSON paths; ordinary Django
  relation paths retain their meaning. Ambiguous declarations fail at build.
- group_key_encoders={"author": encode_author} maps declared unbucketed group
  paths to non-null output identity codecs. A codec must preserve the generated
  GraphQL scalar and identity; it does not change grouping SQL, measures,
  order, filters, or counts. Null values bypass it. Date/number extraction
  buckets cannot use an encoder. Supply matching field_id_decode separately
  if clients filter by an encoded key.
- filter_lookups={"iregex": ("__iregex", False)} maps comparison attribute
  names to Django lookup suffixes and a boolean negation flag. The extension
  reaches list, aggregate, grouped row/count and recursive boolean filters.
  Portable operators cannot be overridden. Unsupported operators fail rather
  than widening results. The computed RowSource vocabulary remains portable.

The lower-level build_aggregate_type and make_aggregate_resolver accept
json_paths; pass the same mapping to both. The where_to_q and comparison_to_q
functions accept the corresponding backend extension as lookups. Existing
resources without these declarations keep their SDL and behavior.
