"""Caller-declared ``<res>_group`` fields filled for a whole page at once.

A consumer declares extra group fields (``group_fields``); each is filled by
one batch callback per groups page and selected response name, so per-group
data (for example each group's first rows) arrives in the same response without
one read per group. Without declared fields the schema is unchanged.

No postponed annotations: Strawberry needs the live generated types.
"""

import dataclasses

import pytest
import strawberry

from strawberry_django_hasura import (
    GroupField,
    GroupFieldBatch,
    InMemoryRowSource,
    hasura_resource,
    hasura_run_query_resource,
)
from tests.demo_schema import Note, NoteWriteBackend, decode_sqid, seed
from tests.models import NoteModel
from tests.test_async import _run_async


@strawberry.type
class TitleSample:
    """A caller-owned type: its snake_case field keeps the schema's naming."""

    first_title: str


def _titles(calls: list[GroupFieldBatch]):
    """Return each group's titles, ordered and limited, from one read."""

    def resolve(batch: GroupFieldBatch) -> list[list[str]]:
        calls.append(batch)
        statuses = [group.key.status for group in batch.groups]
        by_status: dict[str, list[str]] = {status: [] for status in statuses}
        for note in NoteModel.objects.filter(status__in=statuses).order_by(
            "title"
        ):
            by_status[note.status].append(note.title)
        limit = batch.arguments["max_titles"]
        return [by_status[status][:limit] for status in statuses]

    return resolve


def _resource(calls: list[GroupFieldBatch], **fields: GroupField):
    return hasura_resource(
        Note,
        model=NoteModel,
        name="titled_notes",
        filterable=["id", "status"],
        sortable=["title"],
        aggregatable=["word_count"],
        groupable=["status"],
        get_queryset=lambda info: NoteModel.objects.all(),
        write_backend=NoteWriteBackend(),
        id_decode=decode_sqid,
        group_fields=fields
        or {
            "titles": GroupField(
                type=list[str],
                arguments={"max_titles": int},
                defaults={"max_titles": 5},
                resolve=_titles(calls),
            ),
        },
    )


def _schema(resource) -> strawberry.Schema:
    return strawberry.Schema(
        query=resource.query,
        mutation=resource.mutation,
        types=resource.types,
    )


def test_a_group_field_is_filled_for_the_page_by_one_batch(seeded_notes):
    calls: list[GroupFieldBatch] = []
    result = _schema(_resource(calls)).execute_sync(
        """
        query($w: titled_notes_bool_exp) {
          titled_notes_groups(
            group_by: [{ field: STATUS }]
            where: $w
            order_by: [{ field: "status" }]
          ) {
            key { status }
            aggregate { count }
            titles(max_titles: 1)
          }
        }
        """,
        variable_values={"w": {"status": {"_neq": "archived"}}},
    )
    assert result.errors is None, result.errors
    assert result.data["titled_notes_groups"] == [
        {
            "key": {"status": "draft"},
            "aggregate": {"count": 1},
            "titles": ["Bravo"],
        },
        {
            "key": {"status": "published"},
            "aggregate": {"count": 2},
            "titles": ["Alpha"],
        },
    ]
    assert len(calls) == 1
    batch = calls[0]
    assert [group.key.status for group in batch.groups] == [
        "draft",
        "published",
    ]
    assert batch.arguments == {"max_titles": 1}
    assert batch.where is not None
    # The translated spec the library grouped by, not the wire input.
    assert batch.group_by == [("status", None)]


def test_each_response_name_gets_its_own_batch(seeded_notes):
    calls: list[GroupFieldBatch] = []
    result = _schema(_resource(calls)).execute_sync(
        """
        query {
          titled_notes_groups(group_by: [{ field: STATUS }]) {
            key { status }
            one: titles(max_titles: 1)
            all: titles
          }
        }
        """
    )
    assert result.errors is None, result.errors
    published = next(
        group
        for group in result.data["titled_notes_groups"]
        if group["key"]["status"] == "published"
    )
    assert published["one"] == ["Alpha"]
    assert published["all"] == ["Alpha", "Cee"]
    assert sorted(batch.arguments["max_titles"] for batch in calls) == [1, 5]


def test_an_unselected_group_field_costs_nothing(seeded_notes):
    calls: list[GroupFieldBatch] = []
    result = _schema(_resource(calls)).execute_sync(
        "query { titled_notes_groups(group_by: [{ field: STATUS }]) "
        "{ key { status } } }"
    )
    assert result.errors is None, result.errors
    assert calls == []


def test_a_group_field_declares_its_wire_names_and_keeps_caller_types():
    calls: list[GroupFieldBatch] = []
    sample = GroupField(
        type=TitleSample | None,
        resolve=lambda batch: [None] * len(batch.groups),
    )
    sdl = str(_schema(_resource(calls, sample=sample)))
    assert "sample: TitleSample" in sdl
    # The group container is pinned without descending into a caller type.
    assert "firstTitle: String!" in sdl
    sdl = str(_schema(_resource(calls)))
    assert "titles(max_titles: Int! = 5): [String!]!" in sdl


GROUP_TITLES = (
    "query { titled_notes_groups(group_by: [{ field: STATUS }]) { titles } }"
)


def test_a_wrong_value_count_fails_every_group_from_one_batch(seeded_notes):
    calls: list[GroupFieldBatch] = []

    def short(batch: GroupFieldBatch) -> list[list[str] | None]:
        calls.append(batch)
        return [[]]

    titles = GroupField(type=list[str] | None, resolve=short)
    result = _schema(_resource([], titles=titles)).execute_sync(GROUP_TITLES)
    assert result.errors is not None
    assert len(result.errors) == 2
    assert "returned 1 values for 2 groups" in str(result.errors[0])
    assert len(calls) == 1


def _failing(calls: list[GroupFieldBatch]):
    def resolve(batch: GroupFieldBatch) -> list[list[str]]:
        calls.append(batch)
        raise TimeoutError("rows read timed out")

    return GroupField(type=list[str] | None, resolve=resolve)


def test_a_raising_batch_fails_every_group_without_rerunning(seeded_notes):
    calls: list[GroupFieldBatch] = []
    schema = _schema(_resource([], titles=_failing(calls)))
    result = schema.execute_sync(GROUP_TITLES)
    assert result.errors is not None
    assert [str(error.message) for error in result.errors] == [
        "rows read timed out",
        "rows read timed out",
    ]
    assert result.data == {
        "titled_notes_groups": [{"titles": None}, {"titles": None}]
    }
    assert len(calls) == 1


@pytest.mark.django_db(transaction=True)
def test_a_raising_batch_runs_once_under_async_execution():
    seed()
    calls: list[GroupFieldBatch] = []
    schema = _schema(_resource([], titles=_failing(calls)))
    result = _run_async(schema.execute(GROUP_TITLES))
    assert result.errors is not None
    assert len(result.errors) == 2
    assert len(calls) == 1


def test_a_page_without_groups_never_calls_the_batch(seeded_notes):
    calls: list[GroupFieldBatch] = []
    result = _schema(_resource(calls)).execute_sync(
        "query { titled_notes_groups(group_by: [{ field: STATUS }], "
        'where: { status: { _eq: "archived" } }) { titles } }'
    )
    assert result.errors is None, result.errors
    assert result.data == {"titled_notes_groups": []}
    assert calls == []


@pytest.mark.parametrize("name", ["key", "aggregate"])
def test_a_group_field_cannot_shadow_a_group_member(name: str):
    declared = GroupField(type=int, resolve=lambda batch: [])
    with pytest.raises(ValueError, match="collide"):
        _resource([], **{name: declared})


def test_the_page_bookkeeping_is_private_so_its_names_stay_free(seeded_notes):
    index = GroupField(
        type=int, resolve=lambda batch: list(range(len(batch.groups)))
    )
    result = _schema(_resource([], group_index=index)).execute_sync(
        "query { titled_notes_groups(group_by: [{ field: STATUS }], "
        'order_by: [{ field: "status" }]) { group_index } }'
    )
    assert result.errors is None, result.errors
    assert result.data == {
        "titled_notes_groups": [{"group_index": 0}, {"group_index": 1}]
    }
    with pytest.raises(TypeError, match="not a field name"):
        _resource([], __group_index=index)


@pytest.mark.parametrize(
    ("arguments", "defaults"),
    [
        ({"info": int}, {}),
        ({"self": int}, {}),
        ({"root": int}, {}),
        ({"class": int}, {}),
        ({"max-titles": int}, {}),
        ({"__limit": int}, {}),
        ({"limit": "int"}, {}),
        ({"limit": None}, {}),
        ({"limit": int}, {"offset": 0}),
    ],
)
def test_group_field_arguments_are_validated_at_build(arguments, defaults):
    declared = GroupField(
        type=int,
        resolve=lambda batch: [],
        arguments=arguments,
        defaults=defaults,
    )
    with pytest.raises(TypeError):
        _resource([], titles=declared)


@dataclasses.dataclass
class StatusRecord:
    id: str
    status: str


def test_a_row_source_resource_declares_group_fields_too():
    rows = [
        StatusRecord("a", "draft"),
        StatusRecord("b", "published"),
        StatusRecord("c", "published"),
    ]

    @strawberry.type(name="StatusRow")
    class StatusRow:
        id: str
        status: str

    def ids(batch: GroupFieldBatch) -> list[list[str]]:
        return [
            [row.id for row in rows if row.status == group.key.status]
            for group in batch.groups
        ]

    resource = hasura_run_query_resource(
        StatusRow,
        name="status_rows",
        filterable=["id", "status"],
        sortable=["status"],
        source=InMemoryRowSource(lambda info: rows),
        groupable=["status"],
        group_fields={"ids": GroupField(type=list[str], resolve=ids)},
    )
    result = strawberry.Schema(
        query=resource.query, types=resource.types
    ).execute_sync(
        "query { status_rows_groups(group_by: [{ field: STATUS }], "
        'order_by: [{ field: "status" }]) { key { status } ids } }'
    )
    assert result.errors is None, result.errors
    assert result.data["status_rows_groups"] == [
        {"key": {"status": "draft"}, "ids": ["a"]},
        {"key": {"status": "published"}, "ids": ["b", "c"]},
    ]


def test_a_group_field_takes_the_resource_s_own_ordering(seeded_notes):
    calls: list[GroupFieldBatch] = []

    def ordered(batch: GroupFieldBatch) -> list[list[str]]:
        calls.append(batch)
        (order,) = batch.arguments["order_by"]
        descending = order.title.value == "desc"
        return [
            list(
                NoteModel.objects.filter(status=group.key.status)
                .order_by("-title" if descending else "title")
                .values_list("title", flat=True)
            )
            for group in batch.groups
        ]

    titles = GroupField(
        type=list[str],
        arguments=lambda inputs: {"order_by": list[inputs.order_by] | None},
        defaults={"order_by": None},
        resolve=ordered,
    )
    schema = _schema(_resource(calls, titles=titles))
    assert "titles(order_by: [titled_notes_order_by!] = null)" in str(schema)
    result = schema.execute_sync(
        "query { titled_notes_groups(group_by: [{ field: STATUS }], "
        'where: { status: { _eq: "published" } }) '
        "{ titles(order_by: [{ title: desc }]) } }"
    )
    assert result.errors is None, result.errors
    assert result.data["titled_notes_groups"] == [{"titles": ["Cee", "Alpha"]}]
    assert len(calls) == 1
