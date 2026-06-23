# strawberry-django-hasura

Expose Django models over GraphQL in the **Hasura** convention, so the **stock**
[`@refinedev/hasura`](https://refine.dev/docs/data/packages/hasura/) refine data
provider drives a Strawberry/Django backend with **no patching**.

It is a thin adapter: it *composes*
[`strawberry-django`](https://strawberry-django.readthedocs.io) (types, the ORM
seam) and
[`strawberry-django-aggregates`](https://github.com/ang-ee/strawberry-django-aggregates)
and emits the exact GraphQL shape the refine provider speaks. One unmodified
frontend data provider, any Django model. The precise target SDL is in
[`CONTRACT.md`](./CONTRACT.md).

## Why

refine's Hasura provider expects a specific GraphQL shape per resource: a
`notes(where, order_by, limit, offset): [Note!]` list, a `notes_by_pk(id): Note`
detail, `insert_notes_one` / `update_notes_by_pk` / `delete_notes_by_pk`
mutations, `notes_bool_exp` operator objects, `notes_order_by` + the `order_by`
enum, and a `notes_aggregate { aggregate, nodes }` surface. This library emits
all of it from your Strawberry types — you keep the stock provider, no custom
mapping layer.

**The aggregate is free.** Hasura's `aggregate { count, sum {…}, avg {…},
min {…}, max {…} }` *is* the native `<Model>Aggregate` type that
`strawberry-django-aggregates` already emits — so there is **no reshape layer**
(no flat→nested glue to maintain). You wire it; you don't rebuild it.

## Install

```sh
pip install strawberry-django-hasura
# or
uv add strawberry-django-hasura
```

Requires Python 3.14+, Django 6.0+, and a Strawberry/strawberry-django stack
(installed transitively).

## The frontend side — one provider option

Construct the stock provider with `idType: "String"` and the Hasura naming
convention. `idType` declares the id variable type verbatim (`$id: String!`), so
an opaque string id (a sqid) binds through every pk-centric op without a patch;
the refine default is `uuid`, so this option is required for a string id:

```ts
import dataProvider, { GraphQLClient } from "@refinedev/hasura";

const client = new GraphQLClient("https://your.api/graphql");
const dp = dataProvider(client, {
  idType: "String",            // opaque sqid binds as $id: String!
  namingConvention: "hasura-default",
});
```

## Quickstart (backend)

Declare the per-resource Hasura inputs from your Strawberry type, then compose
the adapter's helpers in plain resolvers. (Condensed from
[`tests/demo_schema.py`](./tests/demo_schema.py) /
[`examples/demo_schema.py`](./examples/demo_schema.py), which exercise every
surface — including the opaque-`id` (sqid) boundary.)

```python
import strawberry, strawberry_django
from django.db import models, transaction
from strawberry import UNSET, auto

from strawberry_django_hasura import (
    OrderBy, apply_ordering, build_aggregate_type, hasura_config, input_to_dict,
    make_aggregate_container, make_aggregate_resolver, paginate, where_to_q,
)
from strawberry_django_hasura.comparisons import (
    IDComparison, IntComparison, StringComparison,
)
from .models import Note  # your Django model


@strawberry_django.type(Note)
class NoteType:           # GraphQL type name `Note`
    id: auto
    title: auto
    word_count: auto
    status: auto


@strawberry.input(name="notes_bool_exp")
class NoteBoolExp:
    id: IDComparison | None = UNSET
    title: StringComparison | None = UNSET
    word_count: IntComparison | None = UNSET
    status: StringComparison | None = UNSET
    and_: list["NoteBoolExp"] | None = strawberry.field(name="_and", default=UNSET)
    or_: list["NoteBoolExp"] | None = strawberry.field(name="_or", default=UNSET)
    not_: "NoteBoolExp | None" = strawberry.field(name="_not", default=UNSET)


@strawberry.input(name="notes_order_by")
class NoteOrderBy:                 # per-field input of the `order_by` enum
    title: OrderBy | None = UNSET
    word_count: OrderBy | None = UNSET


@strawberry.input(name="notes_pk_columns_input")
class NotePkColumns:
    id: str                        # String (not ID) — matches idType: "String"


def base_qs():
    # Apply your row-level (e.g. REBAC) scoping here — reads run on this.
    return Note.objects.all()


def filtered(info, where):
    return base_qs().filter(where_to_q(where))


# The free aggregate — the native <Model>Aggregate, wired into the container.
NoteAggregate = build_aggregate_type(Note, name="Note",
                                     aggregate_fields=["word_count"])
aggregate_resolver = make_aggregate_resolver(NoteAggregate)
NoteAggregateContainer = make_aggregate_container(
    "notes_aggregate", NoteType, NoteAggregate,
    filtered_queryset=filtered, aggregate_resolver=aggregate_resolver,
)


@strawberry.type
class Query:
    @strawberry.field(name="notes")
    def notes(self, info: strawberry.Info, where: NoteBoolExp | None = None,
              order_by: list[NoteOrderBy] | None = None,
              limit: int | None = None, offset: int | None = None) -> list[NoteType]:
        qs = apply_ordering(filtered(info, where), order_by)
        return list(paginate(qs, limit, offset))

    @strawberry.field(name="notes_aggregate")
    def notes_aggregate(self, where: NoteBoolExp | None = None) -> NoteAggregateContainer:
        return NoteAggregateContainer(where=where)

    @strawberry.field(name="notes_by_pk")
    def notes_by_pk(self, id: str) -> NoteType | None:   # String to match idType
        return base_qs().filter(pk=id).first()


schema = strawberry.Schema(query=Query, config=hasura_config())
```

`insert_notes_one` / `update_notes_by_pk` / `delete_notes_by_pk` follow the same
pattern, using `input_to_dict` to translate the Hasura `object:` / `_set:`
envelope into model kwargs. Your `<resource>_set_input` is the authoritative
writable-field allowlist (keep server-owned columns out of it); run the
`update_by_pk` read-modify-write inside `transaction.atomic()` and
`save(update_fields=…)` so a patch touches only the columns it set — see
[`tests/demo_schema.py`](./tests/demo_schema.py).

### Opaque ids (sqid)

If your public `id` is an opaque sqid (not the raw pk), keep the output
`id: ID!` field encoded, type every **pk-arg** as `String` (matching
`idType: "String"`), and pass an `id_decode` hook so `where: { id: { _eq } }`
decodes before the lookup:

```python
qs = base_qs().filter(where_to_q(where, id_decode=decode_sqid))
# ...and decode at notes_by_pk / pk_columns: objects.get(pk=decode_sqid(id))
```

The encode/decode stays your concern — the adapter never inspects a value to
guess whether it is a sqid.

## The surfaces

| Surface | Module | What it emits / does |
| --- | --- | --- |
| Filtering | `comparisons`, `filtering` | `<resource>_bool_exp` operator objects → a Django `Q` |
| Ordering | `ordering` | `[<resource>_order_by!]` + the `order_by` enum → `.order_by()` |
| Pagination | `connection` | bare `limit` / `offset` → a queryset slice |
| Aggregation | `aggregation`, `connection` | the **free** `<resource>_aggregate { aggregate, nodes }` — the native `<Model>Aggregate`, zero reshape |
| Mutations | `mutations` | `insert`/`update`/`delete`-by-pk envelope → model kwargs |
| Naming | `naming` | `hasura_config()` — snake_case verbatim on the wire |

## Proof: the stock provider drives it

[`examples/`](./examples/) is a runnable proof that the unmodified
`@refinedev/hasura` provider drives a schema built with this library (`getList`
filter + sort + paging, `getOne`, `create`, `update`, `deleteOne`, and the
aggregate), no patching — using only the `idType: "String"` option.

## Status

Beta (v0.1.0). The public API (`__init__` exports) and the emitted SDL shape
follow [`CONTRACT.md`](./CONTRACT.md) and are stable for early adopters; minor
iteration is expected before a 1.0 stability commitment. Runtime: Python 3.14,
Django 6.0.

## Documentation

- Target SDL contract: [`CONTRACT.md`](./CONTRACT.md)
- Architecture and contributor guide: [`AGENTS.md`](./AGENTS.md)

## License

AGPL-3.0-or-later. See [`LICENSE`](./LICENSE).
