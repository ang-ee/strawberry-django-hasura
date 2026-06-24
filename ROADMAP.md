# Roadmap

Forward-looking and deferred work for `strawberry-django-hasura`. The shipped /
working surfaces are described in [`CONTRACT.md`](./CONTRACT.md); the
architecture and rules are in [`AGENTS.md`](./AGENTS.md).

## Grouping (`<res>_groups`) — preview, NDC-aligned

`<res>_groups` is a **preview** surface (see `CONTRACT.md` → "Grouping — NDC
preview"). It is **not** part of the stock `@refinedev/hasura` contract — that
provider never sends `group_by`. It is shaped to the Hasura v3 / NDC (Native
Data Connector) `groups` semantics and composes `strawberry-django-aggregates`'
**public** grouped surface: the typed `<Model>GroupKey` paired with the **free**
`<Model>Aggregate` (`{ key, aggregate }`, no reshape), via `shape_group_key` +
`shape_aggregate_row` and the `translate_group_by` / `translate_having` /
`translate_order_by` input translators.

**Why "preview":** as of 2026-06 the Hasura **DDN GraphQL** layer has not shipped
a generic `group_by`. NDC the *protocol* fully specifies grouping; the GraphQL
interface is tracked in **hasura/graphql-engine#10786** (open, unassigned). So
the field / argument / type names here may change to track DDN once it publishes
its `group_by` SDL. The stock list / aggregate / CRUD SDL is unaffected
(grouping is purely additive).

### Open items

- **NDC `extraction`-function granularity.** Granularity currently uses the
  aggregates library's fixed `Granularity` enum (TIME `date_trunc` + NUMBER
  `date_part` tracks). NDC models granularity as connector-declared `extraction`
  functions; align the enum / wire to extraction naming when DDN ships its
  GraphQL `group_by`. (Lands partly upstream in `strawberry-django-aggregates`.)
- **Ordered-set aggregate ops error without a `fraction`.** The native
  `<Model>Aggregate` advertises `percentile_cont` / `percentile_disc` (and
  `mode`) on numeric fields, but selecting them — on `<res>_aggregate` *or*
  `<res>_groups` — raises (there is no wire channel for the required
  `fraction`; they are also Postgres-only). Pre-existing since 0.1.0 (it is the
  free aggregate's default operator set, not introduced by grouping). Fix by
  either restricting the Hasura dialect's default operator set to portable,
  no-arg ops, or threading an `op_args` fraction channel through the aggregate
  and groups resolvers.
- **`json_paths` parity in the free-aggregate resolver.** `grouping.py` passes
  `json_paths=builder.json_paths` to `shape_aggregate_row`; `aggregation.py`'s
  free-aggregate resolver does not (its `build_aggregate_type` path keeps no
  builder handle). Dormant while no `json_paths` knob is exposed; wire it
  through if JSON-path *measures* (distinct from the already-shipped JSON
  `_contains` *filter*) are ever enabled.

## Dependency / release

- Grouping requires **`strawberry-django-aggregates >= 0.7.0`** — the release
  that added the public composition seam (`AggregateBuilder.shape_group_key`,
  the public `translate_group_by` / `translate_having` / `translate_order_by`,
  `shape_aggregate_row` + `make_group_order_input` in `__all__`, and
  `group_order_input` on `BuiltAggregates`). 0.7.0 is published on PyPI and
  pinned here as `>=0.7.0`; the earlier dev-only `[tool.uv.sources]` path
  override has been removed.
