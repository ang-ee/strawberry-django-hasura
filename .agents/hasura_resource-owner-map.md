# `hasura_resource(...)` — architecture / owner map

One-call declarative builder that assembles the full Hasura GraphQL surface for
a Django model by **composing** the existing primitives (no fork, no reshape).

## Owner map (find-the-owner)

| Fact | Owner | How the builder uses it |
| --- | --- | --- |
| A column's python/GraphQL type | `strawberry-django`'s `field_type_map` | `_column_python_type` asks the owner (MRO walk for subclasses); the node type is built from the same map, so insert/`_set`/comparison types match it by construction. An unmapped field raises (fail-fast, like `filtering`). |
| Which Hasura `*_comparison_exp` a scalar gets | the **adapter** (its own filter vocabulary) | `_COMPARISON_FOR_TYPE` maps the owner-derived python type → the `*Comparison` input; the fixed refine `id` wire name (`_ID_WIRE_NAME`) → `IDComparison` |
| Insert/`_set` writable columns + their GraphQL defaults | the Django field (`editable`, `primary_key`, `auto_now*`, `has_default`/`get_default`) | derive the insert/set input fields from the model's editable, non-pk, non-auto concrete fields |
| `where` → `Q`, ordering, paging | existing `where_to_q` / `apply_ordering` / `paginate` | composed unchanged |
| the free `<Model>Aggregate` + container | existing `build_aggregate_type` / `make_aggregate_resolver` / `make_aggregate_container` | composed unchanged; folded-in `_pin_snake_wire_names` pins its field names |
| row scoping (REBAC) on reads | caller's `get_queryset(info)` | reads run on it |
| authorized writes + relation coercion | caller's `write_backend.create/update/delete` | writes dispatch to it |
| sqid ⇄ pk decode for the `id` arg | caller's `id_decode` | passed to `where_to_q`; applied at by-pk / pk_columns |
| snake_case wire names (no schema-wide converter) | the builder | pins `graphql_name` on roots, args, generated input fields, and aggregate-type fields |

## Why the builder pins snake names itself

A consuming schema (Angee) installs the default camelCase converter for the
whole schema and has no per-surface seam, so `hasura_config()` cannot be used.
The builder therefore pins each generated field's / argument's `graphql_name`
to its snake_case python name — the same wire effect, scoped to this resource's
types. `hasura_config()` stays an optional end-state convenience for a
schema dedicated to a single dialect.

## Self-referential inputs

`<res>_bool_exp` references itself (`_and`/`_or`/`_not`). The generated types are
hosted in a per-resource synthetic module so strawberry resolves the forward
refs at schema-build time.
</content>
