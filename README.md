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

Define your Strawberry type and an authorized-write backend, then call
`hasura_resource(...)` once — it assembles the *whole* Hasura surface (inputs,
the `notes` / `notes_aggregate` / `notes_by_pk` queries, the
`insert`/`update`/`delete`-by-pk mutations, and the free `<Model>Aggregate`) and
pins the snake_case wire names itself. (Condensed from
[`tests/demo_schema.py`](./tests/demo_schema.py), which exercises every surface
— including the opaque-`id` (sqid) boundary.)

```python
import strawberry, strawberry_django
from strawberry import auto

from strawberry_django_hasura import hasura_resource
from .models import Note  # your Django model


@strawberry_django.type(Note)
class NoteType:           # GraphQL type name `Note`
    title: auto
    word_count: auto
    status: auto

    @strawberry.field
    def id(self) -> strawberry.ID:   # public id (e.g. a sqid)
        return strawberry.ID(encode_sqid(self.pk))


def get_queryset(info):
    # Apply your row-level (e.g. REBAC) scoping here — reads + the aggregate
    # run on this; the builder applies the Hasura `where` on top.
    return Note.objects.all()


class NoteWriteBackend:               # the authorized-write seam (a Protocol)
    def create(self, info, data):     # insert_notes_one(object:)
        return Note.objects.create(**data)
    def update(self, info, pk, data): # update_notes_by_pk(pk_columns:, _set:)
        obj = Note.objects.get(pk=decode_sqid(pk))
        for k, v in data.items(): setattr(obj, k, v)
        obj.save(update_fields=[*data]); return obj
    def delete(self, info, pk):       # delete_notes_by_pk(id:)
        obj = Note.objects.filter(pk=decode_sqid(pk)).first()
        if obj: obj.delete()
        return obj


resource = hasura_resource(
    NoteType,
    model=Note,
    name="notes",
    filterable=["id", "title", "word_count", "status"],
    sortable=["title", "word_count"],
    aggregatable=["word_count"],
    get_queryset=get_queryset,
    write_backend=NoteWriteBackend(),
    id_decode=decode_sqid,            # omit for a raw-pk project
)

schema = strawberry.Schema(
    query=resource.query, mutation=resource.mutation, types=resource.types,
)
```

`hasura_resource` derives the comparison / order scalar of each column from the
**Django field**, and the `insert` / `_set` writable fields from the model's
editable, non-pk, non-auto concrete fields plus editable many-to-many relation
arrays. Because it pins each wire name itself, the
resource is correct on a stock *camelCase* schema (e.g. an Angee schema) with no
schema-wide converter — `hasura_config()` (below) stays an optional convenience
for a schema dedicated to a single dialect.

### The primitives (custom assembly)

`hasura_resource` composes the five surface primitives, which remain public for
a resource that needs custom shaping (a non-derivable input, a bespoke
resolver): `where_to_q` / `apply_ordering` / `paginate` /
`build_aggregate_type` + `make_aggregate_resolver` + `make_aggregate_container`
/ `input_to_dict`, the `*Comparison` inputs, the `OrderBy` enum, and
`hasura_config()`. Wire them in plain resolvers (as the builder does) when you
step off the one-call path.

### Opaque ids (sqid)

If your public `id` is an opaque sqid (not the raw pk), keep the output
`id: ID!` field encoded, and pass `id_decode` to `hasura_resource` — the builder
decodes `where: { id: { _eq } }` and `notes_by_pk` / `pk_columns.id` before the
lookup, and the pk-arg surface is typed GraphQL `String` (matching
`idType: "String"`). The encode/decode and the per-write decode (in your
`write_backend`) stay your concern — the adapter never inspects a value to guess
whether it is a sqid.

## The surfaces

| Surface | Module | What it emits / does |
| --- | --- | --- |
| **Resource builder** | `resource` | `hasura_resource(...)` — assembles the whole surface in one call, snake-naming baked in |
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
