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
- composition: `_and: [notes_bool_exp!]`, `_or: [notes_bool_exp!]`,
  `_not: notes_bool_exp`

refine's `hasuraFilterOperatorMappings` sends `eq→_eq`, `ne→_neq`,
`lt/gt/lte/gte`, `in→_in`, `nin→_nin`, `contains→_ilike`, `containss→_like`,
`null/nnull→_is_null` (+ Postgres regex/similar for `startswith`/`endswith`).
Maps to Django `Q`: `_eq→exact`, `_neq→~exact`, `_in→in`, `_nin→~in`,
`_like→contains`, `_ilike→icontains`, `_gt→gt`, …, `_is_null:true→isnull`.

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
