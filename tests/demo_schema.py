"""Toy ``Note`` schema (Hasura-shaped via the adapter), the test fixture.

Exercises every surface this library exposes — the ``notes`` list (where +
order_by + limit/offset), the free ``notes_aggregate { aggregate nodes }``,
``notes_by_pk``, and ``insert_notes_one`` / ``update_notes_by_pk`` /
``delete_notes_by_pk`` — over a single in-memory model, built in ONE call to
:func:`~strawberry_django_hasura.hasura_resource`. The builder assembles the
whole Hasura surface (inputs, query/mutation roots, the free aggregate) and
pins the snake_case wire names itself, so this fixture reads like
``CONTRACT.md`` with no per-resource hand-wiring.

It also demonstrates the **sqid boundary**: the public ``id`` is an opaque sqid
(the raw pk is hidden), and the pk-arg surface (``notes_by_pk(id:)``,
``pk_columns.id``, ``where.id._eq``) is GraphQL ``String`` — refine's
``idType: "String"`` declares the id variable ``$id: String!`` verbatim, never
``ID``. The encode/decode is the consumer's concern; the builder takes the
``id_decode`` hook (it decodes at the ``where`` / by-pk boundary) and the
``NoteWriteBackend`` decodes at the write boundary.

The caller MUST configure Django settings + call ``django.setup()`` BEFORE
importing this module — ``hasura_resource`` introspects the model relation tree
(``AggregateBuilder``), which needs a fully populated app registry;
``tests/conftest.py`` does the setup in ``pytest_configure``. The Django model
is ``tests.models.NoteModel``; the GraphQL type is ``Note``.

The runnable ``examples/demo_schema.py`` mirrors this module (standalone, for
``server.py``); keep the two in sync when the ``Note`` shape changes.
"""

from __future__ import annotations

from typing import Any

import strawberry
import strawberry_django
from django.db import models, transaction
from strawberry import auto

from strawberry_django_hasura import hasura_resource
from tests.models import NoteModel

# --- sqid boundary -----------------------------------------------------------
# Toy reversible opaque-string codec over the integer pk (the real Angee uses
# ``sqids``). Enough to prove the stock provider roundtrips an opaque string id
# through the pk-centric ops with only ``idType: "String"``.
_SQID_SALT = 1000


def encode_sqid(pk: int) -> str:
    return f"sq{pk + _SQID_SALT}"


def decode_sqid(sqid: str) -> int:
    return int(str(sqid).removeprefix("sq")) - _SQID_SALT


@strawberry_django.type(NoteModel)
class Note:  # GraphQL type name is `Note`
    title: auto
    word_count: auto
    is_starred: auto
    status: auto
    updated_at: auto

    @strawberry.field
    def id(self) -> strawberry.ID:  # public id == sqid
        return strawberry.ID(encode_sqid(self.pk))


# --- queryset seam (apply REBAC / row-level scoping here) --------------------


def get_queryset(info: strawberry.Info) -> models.QuerySet[NoteModel]:
    """The row-scoped base queryset reads + the aggregate run on.

    A real consumer applies REBAC / ``filter(owner=...)`` row scope here; the
    builder applies the Hasura ``where`` on top. The toy is permission-naive.
    """
    return NoteModel.objects.all()


# --- the authorized-write seam (insert / update / delete by pk) --------------


class NoteWriteBackend:
    """Persist the Hasura writes over the toy ``Note`` (the bare ORM).

    The builder hands each write the already-decoded input dict; this backend
    owns persistence + the sqid⇄pk decode at the write boundary. A real
    consumer wraps its authorized CRUD machinery (REBAC gate, ``full_clean``,
    relation coercion) here instead.
    """

    def create(self, info: strawberry.Info, data: dict[str, Any]) -> NoteModel:
        return NoteModel.objects.create(**data)

    def update(
        self, info: strawberry.Info, pk: str, data: dict[str, Any]
    ) -> NoteModel:
        # ``data`` is the writable-field allowlist (the unset ``_set`` columns
        # are already dropped); patch only those via ``update_fields`` so an
        # omitted column isn't clobbered, and add the ``auto_now``
        # ``updated_at`` (Django skips it for unlisted columns). ``atomic``
        # gives atomicity.
        with transaction.atomic():
            obj = NoteModel.objects.get(pk=decode_sqid(pk))
            for key, value in data.items():
                setattr(obj, key, value)
            obj.save(update_fields=[*data, "updated_at"])
        return obj

    def delete(self, info: strawberry.Info, pk: str) -> NoteModel | None:
        with transaction.atomic():
            obj = NoteModel.objects.filter(pk=decode_sqid(pk)).first()
            if obj is None:
                return None
            deleted_pk = obj.pk
            obj.delete()
        # Django nulls pk on delete; restore so the response resolves the
        # id (sqid).
        obj.pk = deleted_pk
        return obj


# --- the whole Hasura surface, in one call -----------------------------------

resource = hasura_resource(
    Note,
    model=NoteModel,
    name="notes",
    filterable=["id", "title", "word_count", "is_starred", "status"],
    sortable=["title", "word_count", "updated_at"],
    aggregatable=["word_count"],
    get_queryset=get_queryset,
    write_backend=NoteWriteBackend(),
    id_decode=decode_sqid,
)


# NOTE: no ``config=hasura_config()``. The builder pins the snake_case wire
# names itself (per field / argument / aggregate-type field), so the resource
# is correct on a stock *camelCase* schema — proving a consumer like Angee,
# which cannot install a schema-wide snake-case converter, gets snake_case
# wire names for the Hasura surface for free. ``hasura_config()`` stays an
# optional convenience for a schema dedicated to a single dialect.
schema = strawberry.Schema(
    query=resource.query,
    mutation=resource.mutation,
    types=resource.types,
)


def seed() -> None:
    if NoteModel.objects.exists():
        return
    NoteModel.objects.create(
        title="Alpha", word_count=10, is_starred=True, status="published"
    )
    NoteModel.objects.create(
        title="Bravo", word_count=30, is_starred=False, status="draft"
    )
    NoteModel.objects.create(
        title="Cee", word_count=20, is_starred=True, status="published"
    )
