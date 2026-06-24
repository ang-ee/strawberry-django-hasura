"""Self-contained toy `Note` schema (Hasura-shaped via the adapter).

Mirrors `tests/demo_schema.py` but standalone for `server.py` (no `tests` app):
the model registers against the already-installed `contenttypes` app so it can
be built right after `django.setup()`. Demonstrates every surface the stock
`@refinedev/hasura` provider drives — the `notes` list (where / order_by /
limit / offset), the free `notes_aggregate { aggregate, nodes }`,
`notes_by_pk`, and the `insert`/`update`/`delete`-by-pk mutations — plus the
sqid `id` boundary.

The caller MUST configure Django settings + call `django.setup()` BEFORE
importing this module (`AggregateBuilder` introspects the model relation tree);
`server.py` does this. Keep in sync with `tests/demo_schema.py`.
"""

from __future__ import annotations

import strawberry
import strawberry_django
from django.db import models, transaction
from strawberry import UNSET, auto

from strawberry_django_hasura import (
    OrderBy,
    apply_ordering,
    build_aggregate_type,
    hasura_config,
    input_to_dict,
    make_aggregate_container,
    make_aggregate_resolver,
    paginate,
    where_to_q,
)
from strawberry_django_hasura.comparisons import (
    BooleanComparison,
    IDComparison,
    IntComparison,
    StringComparison,
)

# --- sqid boundary (toy reversible codec over the integer pk) ----------------
_SQID_SALT = 1000


def encode_sqid(pk: int) -> str:
    return f"sq{pk + _SQID_SALT}"


def decode_sqid(sqid: str) -> int:
    return int(str(sqid).removeprefix("sq")) - _SQID_SALT


class NoteModel(models.Model):
    title = models.CharField(max_length=200)
    word_count = models.IntegerField(default=0)
    is_starred = models.BooleanField(default=False)
    status = models.CharField(max_length=20, default="draft")
    metadata = models.JSONField(default=dict, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "contenttypes"  # an installed app, so the toy registers


@strawberry_django.type(NoteModel)
class Note:
    title: auto
    word_count: auto
    is_starred: auto
    status: auto
    metadata: auto
    updated_at: auto

    @strawberry.field
    def id(self) -> strawberry.ID:  # public id == sqid
        return strawberry.ID(encode_sqid(self.pk))


@strawberry.input(name="notes_bool_exp")
class NoteBoolExp:
    id: IDComparison | None = UNSET
    title: StringComparison | None = UNSET
    word_count: IntComparison | None = UNSET
    is_starred: BooleanComparison | None = UNSET
    status: StringComparison | None = UNSET
    and_: list[NoteBoolExp] | None = strawberry.field(
        name="_and", default=UNSET
    )
    or_: list[NoteBoolExp] | None = strawberry.field(name="_or", default=UNSET)
    not_: NoteBoolExp | None = strawberry.field(name="_not", default=UNSET)


@strawberry.input(name="notes_order_by")
class NoteOrderBy:
    title: OrderBy | None = UNSET
    word_count: OrderBy | None = UNSET
    updated_at: OrderBy | None = UNSET


@strawberry.input(name="notes_insert_input")
class NoteInsertInput:
    title: str
    word_count: int = 0
    is_starred: bool = False
    status: str = "draft"


@strawberry.input(name="notes_set_input")
class NoteSetInput:
    title: str | None = UNSET
    word_count: int | None = UNSET
    is_starred: bool | None = UNSET
    status: str | None = UNSET


@strawberry.input(name="notes_pk_columns_input")
class NotePkColumns:
    id: str  # String (not ID) — matches refine idType: "String"


def base_qs() -> models.QuerySet[NoteModel]:
    return NoteModel.objects.all()


def _filtered(
    info: strawberry.Info, where: NoteBoolExp | None
) -> models.QuerySet[NoteModel]:
    return base_qs().filter(where_to_q(where, id_decode=decode_sqid))


NoteAggregate = build_aggregate_type(
    NoteModel, name="Note", aggregate_fields=["word_count"]
)
_aggregate_resolver = make_aggregate_resolver(NoteAggregate)
NoteAggregateContainer = make_aggregate_container(
    "notes_aggregate",
    Note,
    NoteAggregate,
    filtered_queryset=_filtered,
    aggregate_resolver=_aggregate_resolver,
)


@strawberry.type
class Query:
    @strawberry.field(name="notes")
    def notes(
        self,
        info: strawberry.Info,
        where: NoteBoolExp | None = None,
        order_by: list[NoteOrderBy] | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[Note]:
        qs = apply_ordering(_filtered(info, where), order_by)
        return list(paginate(qs, limit, offset))

    @strawberry.field(name="notes_aggregate")
    def notes_aggregate(
        self, where: NoteBoolExp | None = None
    ) -> NoteAggregateContainer:  # type: ignore[valid-type]
        return NoteAggregateContainer(where=where)

    @strawberry.field(name="notes_by_pk")
    def notes_by_pk(self, id: str) -> Note | None:  # String to match idType
        return base_qs().filter(pk=decode_sqid(id)).first()


@strawberry.type
class Mutation:
    @strawberry.mutation(name="insert_notes_one")
    def insert_notes_one(self, object: NoteInsertInput) -> Note:
        return NoteModel.objects.create(**input_to_dict(object))

    @strawberry.mutation(name="update_notes_by_pk")
    def update_notes_by_pk(
        self, pk_columns: NotePkColumns, _set: NoteSetInput
    ) -> Note:
        fields = input_to_dict(_set)
        with transaction.atomic():
            obj = NoteModel.objects.get(pk=decode_sqid(pk_columns.id))
            for key, value in fields.items():
                setattr(obj, key, value)
            obj.save(update_fields=[*fields, "updated_at"])
        return obj

    @strawberry.mutation(name="delete_notes_by_pk")
    def delete_notes_by_pk(self, id: str) -> Note | None:
        with transaction.atomic():
            obj = NoteModel.objects.filter(pk=decode_sqid(id)).first()
            if obj is None:
                return None
            deleted_pk = obj.pk
            obj.delete()
        obj.pk = deleted_pk  # restore so the response resolves id (sqid)
        return obj


schema = strawberry.Schema(
    query=Query, mutation=Mutation, config=hasura_config()
)


def create_table() -> None:
    from django.db import connection

    with connection.schema_editor() as editor:
        editor.create_model(NoteModel)


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
