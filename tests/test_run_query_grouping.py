"""Grouping a row-source resource — the same ``<res>_groups`` contract.

A computed resource built with ``groupable=[...]`` must be indistinguishable
from a model resource on the wire: the same roots, arguments, group types and
metadata. Execution groups the rows the source returns for ``where`` in
memory through the aggregate owner's ``compute_row_aggregation``.

No postponed annotations: Strawberry needs the live generated types.
"""

import dataclasses
import datetime
import decimal
import enum
import re
from typing import Any

import pytest
import strawberry
import strawberry_django
from strawberry import auto
from strawberry.scalars import JSON
from strawberry.types import get_object_definition
from strawberry_django_aggregates import AggregateError

from strawberry_django_hasura import (
    InMemoryRowSource,
    hasura_resource,
    hasura_run_query_resource,
)
from tests.demo_schema import NoteWriteBackend
from tests.models import NoteModel


@strawberry.enum
class Visibility(enum.Enum):
    PUBLIC = "public"
    INTERNAL = "internal"


@dataclasses.dataclass
class FieldRow:
    id: str
    name: str
    kind: str
    is_relation: bool
    relation_target: str | None
    model: str
    addon: str
    created: datetime.datetime
    visibility: Visibility


@strawberry.type(name="ComputedField")
class ComputedField:
    id: str
    name: str
    kind: str
    is_relation: bool
    relation_target: str | None
    model: str
    addon: str
    created: datetime.datetime
    visibility: Visibility


def _at(month: int, day: int) -> datetime.datetime:
    return datetime.datetime(2026, month, day, 9, tzinfo=datetime.UTC)


ROWS = [
    FieldRow(
        "auth.user.id",
        "id",
        "BigAutoField",
        False,
        None,
        "auth.user",
        "auth",
        _at(5, 4),
        Visibility.PUBLIC,
    ),
    FieldRow(
        "auth.user.groups",
        "groups",
        "ManyToManyField",
        True,
        "auth.group",
        "auth.user",
        "auth",
        _at(5, 9),
        Visibility.PUBLIC,
    ),
    FieldRow(
        "auth.group.name",
        "name",
        "CharField",
        False,
        "",
        "auth.group",
        "auth",
        _at(6, 1),
        Visibility.INTERNAL,
    ),
    FieldRow(
        "notes.note.author",
        "author",
        "ForeignKey",
        True,
        "auth.user",
        "notes.note",
        "notes",
        _at(6, 2),
        Visibility.PUBLIC,
    ),
    FieldRow(
        "notes.note.title",
        "title",
        "CharField",
        False,
        "",
        "notes.note",
        "notes",
        _at(6, 3),
        Visibility.INTERNAL,
    ),
]

GROUPABLE = [
    "model",
    "addon",
    "kind",
    "is_relation",
    "relation_target",
    "created",
    "visibility",
]


class RecordingSource(InMemoryRowSource):
    """The in-memory source, recording every ``query`` page it serves."""

    def __init__(self, rows: list[FieldRow]) -> None:
        super().__init__(lambda info: rows)
        self.calls: list[dict[str, Any]] = []

    def query(
        self,
        info: strawberry.Info,
        *,
        where: Any,
        order_by: list[Any] | None,
        limit: int | None,
        offset: int | None,
    ) -> list[Any]:
        self.calls.append({"limit": limit, "offset": offset})
        return super().query(
            info, where=where, order_by=order_by, limit=limit, offset=offset
        )


def _resource(name="computed_fields", source=None, **options):
    return hasura_run_query_resource(
        ComputedField,
        name=name,
        filterable=["id", "name", "kind", "model", "addon", "is_relation"],
        sortable=["name", "model"],
        source=source or InMemoryRowSource(lambda info: ROWS),
        groupable=GROUPABLE,
        **options,
    )


def _schema(resource):
    return strawberry.Schema(query=resource.query, types=resource.types)


def _run(query, resource=None, **variables):
    result = _schema(resource or _resource()).execute_sync(
        query, variable_values=variables or None
    )
    assert result.errors is None, result.errors
    return result.data


def _graphql_name(strawberry_type):
    return get_object_definition(strawberry_type).name


def _group_type_names(resource) -> list[str]:
    """Every grouped type name, read from the built resource."""
    spec = get_object_definition(resource.group_by_spec_type)
    field_enum = next(f for f in spec.fields if f.python_name == "field")
    return [
        _graphql_name(resource.group_type),
        _graphql_name(resource.group_key_type),
        _graphql_name(resource.group_by_spec_type),
        field_enum.type.name,
        _graphql_name(resource.having_type),
        _graphql_name(resource.group_order_type),
    ]


def _group_contract(resource) -> dict[str, str]:
    """The grouped root signatures plus every grouped type block.

    The group's ``aggregate`` field names the resource's own aggregate type,
    which each builder names independently; it is replaced by a placeholder
    after checking it really is that resource's aggregate type.
    """
    sdl = _schema(resource).as_str()
    contract = {}
    for root in (resource.groups_root, resource.groups_count_root):
        match = re.search(rf"^  {root}\(.*$", sdl, re.M)
        assert match is not None, root
        contract[root] = match[0]
    for name in _group_type_names(resource):
        match = re.search(
            rf"^(?:type|input|enum) {name} \{{.*?^\}}", sdl, re.M | re.S
        )
        assert match is not None, name
        contract[name] = match[0]
    group = _graphql_name(resource.group_type)
    own_aggregate = f"aggregate: {_graphql_name(resource.aggregate_type)}!"
    assert own_aggregate in contract[group]
    contract[group] = contract[group].replace(
        own_aggregate, "aggregate: <resource aggregate>!"
    )
    return contract


# --- SDL: indistinguishable from a model resource ---------------------------


def test_groups_sdl_matches_a_model_resource():
    columns = ["status", "is_starred", "word_count", "price", "updated_at"]

    @strawberry_django.type(NoteModel)
    class ParityNote:
        id: auto
        status: auto

    @strawberry.type
    class ParityRow:
        id: str
        title: str
        status: str
        is_starred: bool
        word_count: int
        price: decimal.Decimal
        updated_at: datetime.datetime

    model_resource = hasura_resource(
        ParityNote,
        model=NoteModel,
        name="parity_notes",
        filterable=["id", "status"],
        sortable=["status"],
        aggregatable=[],
        groupable=columns,
        get_queryset=lambda info: NoteModel.objects.all(),
        write_backend=NoteWriteBackend(),
        insert=False,
        update=False,
        delete=False,
    )
    row_resource = hasura_run_query_resource(
        ParityRow,
        name="parity_notes",
        filterable=["id", "status"],
        sortable=["status"],
        source=InMemoryRowSource(lambda info: []),
        groupable=columns,
    )
    row_contract = _group_contract(row_resource)

    assert _group_type_names(row_resource) == _group_type_names(model_resource)
    assert row_contract == _group_contract(model_resource)
    assert row_contract["parity_notes_groups"].startswith(
        "  parity_notes_groups(group_by: ["
    )
    # The group aggregate is the resource's own aggregate type on both paths
    # (checked in ``_group_contract``); for a row source that is the
    # count-only payload ``<res>_aggregate`` already exposes.
    aggregate = _graphql_name(row_resource.aggregate_type)
    row_sdl = _schema(row_resource).as_str()
    assert re.search(rf"type {aggregate} \{{\s+count: Int!\s+\}}", row_sdl)


def test_groupable_resource_exposes_group_metadata_and_types():
    resource = _resource()

    assert resource.groups_root == "computed_fields_groups"
    assert resource.groups_count_root == "computed_fields_groups_count"
    # Generated names follow the model path's prefix rule exactly: Strawberry
    # camel-cases a snake prefix the same way for both builders.
    assert _group_type_names(resource) == [
        "computed_fields_group",
        "computedFieldsgroupkey",
        "computedFieldsgroupbyspec",
        "computed_fieldsGroupableField",
        "computedFieldshaving",
        "computedFieldsgrouporder",
    ]
    assert {
        resource.group_type,
        resource.group_key_type,
        resource.group_by_spec_type,
        resource.group_order_type,
        resource.having_type,
    } <= set(resource.types)
    row_model = resource.row_model
    assert row_model is not None
    assert row_model._meta.abstract
    assert [field.name for field in row_model._meta.get_fields()] == GROUPABLE
    created = row_model._meta.get_field("created")
    assert type(created).__name__ == "DateTimeField"


def test_resource_without_groupable_is_unchanged():
    resource = hasura_run_query_resource(
        ComputedField,
        name="plain_fields",
        filterable=["id"],
        sortable=["name"],
        source=InMemoryRowSource(lambda info: ROWS),
    )

    assert resource.groups_root is None
    assert resource.groups_count_root is None
    assert resource.row_model is None
    assert "plain_fields_groups" not in _schema(resource).as_str()


# --- execution ---------------------------------------------------------------


def test_groups_count_rows_per_key():
    data = _run(
        """
        query {
          computed_fields_groups(group_by: [{ field: MODEL }]) {
            key { model }
            aggregate { count }
          }
        }
        """
    )

    assert [
        (group["key"]["model"], group["aggregate"]["count"])
        for group in data["computed_fields_groups"]
    ] == [("auth.group", 1), ("auth.user", 2), ("notes.note", 2)]


def test_where_filters_rows_before_grouping():
    data = _run(
        """
        query($where: computed_fields_bool_exp) {
          computed_fields_groups(
            group_by: [{ field: KIND }], where: $where
          ) { key { kind } aggregate { count } }
          computed_fields_groups_count(
            group_by: [{ field: KIND }], where: $where
          )
        }
        """,
        where={"addon": {"_eq": "auth"}, "is_relation": {"_eq": False}},
    )

    assert [g["key"]["kind"] for g in data["computed_fields_groups"]] == [
        "BigAutoField",
        "CharField",
    ]
    assert data["computed_fields_groups_count"] == 2


def test_null_and_empty_string_stay_distinct_buckets():
    data = _run(
        """
        query {
          computed_fields_groups(group_by: [{ field: RELATION_TARGET }]) {
            key { relation_target }
            aggregate { count }
          }
        }
        """
    )

    assert [
        (group["key"]["relation_target"], group["aggregate"]["count"])
        for group in data["computed_fields_groups"]
    ] == [("", 2), ("auth.group", 1), ("auth.user", 1), (None, 1)]


def test_multi_level_groups_having_order_and_paging():
    query = """
        query($having: computedFieldshaving, $limit: Int, $offset: Int) {
          computed_fields_groups(
            group_by: [{ field: ADDON }, { field: IS_RELATION }]
            having: $having
            order_by: [
              { field: "count", direction: DESC }
              { field: "addon", direction: ASC }
            ]
            limit: $limit
            offset: $offset
          ) { key { addon is_relation } aggregate { count } }
          computed_fields_groups_count(
            group_by: [{ field: ADDON }, { field: IS_RELATION }]
            having: $having
          )
        }
    """
    every = _run(query)
    page = _run(query, limit=2, offset=1)
    having = _run(query, having={"count_gt": 1})

    def keys(data):
        return [
            (
                g["key"]["addon"],
                g["key"]["is_relation"],
                g["aggregate"]["count"],
            )
            for g in data["computed_fields_groups"]
        ]

    assert keys(every) == [
        ("auth", False, 2),
        ("auth", True, 1),
        ("notes", False, 1),
        ("notes", True, 1),
    ]
    assert every["computed_fields_groups_count"] == 4
    assert keys(page) == keys(every)[1:3]
    assert page["computed_fields_groups_count"] == 4
    assert keys(having) == [("auth", False, 2)]
    assert having["computed_fields_groups_count"] == 1


def test_date_axes_bucket_with_range_and_number_parts():
    data = _run(
        """
        query {
          month: computed_fields_groups(
            group_by: [{ field: CREATED, granularity: MONTH }]
          ) {
            key { created_month created_month_range { from to } }
            aggregate { count }
          }
          weekday: computed_fields_groups(
            group_by: [{ field: CREATED, granularity: DAY_OF_WEEK }]
          ) { key { created_day_of_week } aggregate { count } }
        }
        """
    )

    assert [
        (
            g["key"]["created_month"],
            g["key"]["created_month_range"]["to"],
            g["aggregate"]["count"],
        )
        for g in data["month"]
    ] == [
        ("2026-05-01T00:00:00+00:00", "2026-06-01T00:00:00+00:00", 2),
        ("2026-06-01T00:00:00+00:00", "2026-07-01T00:00:00+00:00", 3),
    ]
    # 2026-05-04 is a Monday, 05-09 a Saturday, 06-01..03 Monday..Wednesday.
    assert [
        (g["key"]["created_day_of_week"], g["aggregate"]["count"])
        for g in data["weekday"]
    ] == [(1, 2), (2, 1), (3, 1), (6, 1)]


def test_enum_column_groups_into_a_typed_enum_key():
    resource = _resource()
    data = _run(
        """
        query {
          computed_fields_groups(group_by: [{ field: VISIBILITY }]) {
            key { visibility }
            aggregate { count }
          }
        }
        """,
        resource=resource,
    )

    assert [
        (g["key"]["visibility"], g["aggregate"]["count"])
        for g in data["computed_fields_groups"]
    ] == [("INTERNAL", 2), ("PUBLIC", 3)]
    key = _group_contract(resource)["computedFieldsgroupkey"]
    assert "visibility: computed_fieldsVisibility" in key


def test_empty_group_by_is_one_group_with_the_row_total():
    data = _run(
        """
        query {
          computed_fields_groups(group_by: []) { aggregate { count } }
          computed_fields_groups_count(group_by: [])
        }
        """
    )

    assert data["computed_fields_groups"] == [{"aggregate": {"count": 5}}]
    assert data["computed_fields_groups_count"] == 1


def test_max_rows_does_not_cap_grouping_and_max_groups_caps_the_page():
    source = RecordingSource(ROWS)
    resource = _resource(source=source, max_rows=1, max_groups=2)

    data = _run(
        """
        query {
          computed_fields_groups(group_by: [{ field: MODEL }], limit: 50) {
            key { model }
            aggregate { count }
          }
          computed_fields_groups_count(group_by: [{ field: MODEL }])
        }
        """,
        resource=resource,
    )

    assert [
        (g["key"]["model"], g["aggregate"]["count"])
        for g in data["computed_fields_groups"]
    ] == [("auth.group", 1), ("auth.user", 2)]
    assert data["computed_fields_groups_count"] == 3
    # Both roots read every matching row: neither max_rows nor the group
    # page reaches the source.
    assert source.calls == [
        {"limit": None, "offset": None},
        {"limit": None, "offset": None},
    ]


def test_group_key_encoders_shape_output_only():
    resource = _resource(group_key_encoders={"addon": str.upper})
    data = _run(
        """
        query {
          computed_fields_groups(
            group_by: [{ field: ADDON }]
            order_by: [{ field: "addon", direction: DESC }]
          ) { key { addon } aggregate { count } }
        }
        """,
        resource=resource,
    )

    assert [
        (g["key"]["addon"], g["aggregate"]["count"])
        for g in data["computed_fields_groups"]
    ] == [("NOTES", 2), ("AUTH", 3)]


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (
            'group_by: [{ field: MODEL }], order_by: [{ field: "name" }]',
            "Order term",
        ),
        ("group_by: [{ field: NAME }]", "GroupableField"),
        ("group_by: [{ field: KIND, granularity: MONTH }]", "Granularity"),
        ("group_by: [{ field: MODEL }], limit: -1", "limit"),
    ],
)
def test_invalid_group_requests_fail_loud(arguments, message):
    result = _schema(_resource()).execute_sync(
        f"query {{ computed_fields_groups({arguments}) "
        "{ aggregate { count } } }"
    )

    assert result.errors is not None
    assert message in str(result.errors[0])


# --- build-time validation ---------------------------------------------------


def test_unknown_groupable_column_fails_at_build():
    with pytest.raises(TypeError, match="unknown node field"):
        hasura_run_query_resource(
            ComputedField,
            name="bad_fields",
            filterable=["id"],
            sortable=[],
            source=InMemoryRowSource(lambda info: []),
            groupable=["missing"],
        )


@strawberry.type
class UnsupportedRow:
    id: str
    tags: list[str]
    payload: JSON
    ref: strawberry.ID


@pytest.mark.parametrize("column", ["tags", "payload", "ref"])
def test_unsupported_groupable_column_type_fails_at_build(column):
    with pytest.raises(TypeError, match=f"cannot group.*'{column}'"):
        hasura_run_query_resource(
            UnsupportedRow,
            name=f"unsupported_{column}",
            filterable=["id"],
            sortable=[],
            source=InMemoryRowSource(lambda info: []),
            groupable=[column],
        )


def test_group_key_encoder_must_name_a_groupable_column():
    with pytest.raises(ValueError, match="groupable"):
        _resource(name="encoded_fields", group_key_encoders={"name": str})


def test_negative_max_groups_fails_at_build():
    with pytest.raises(ValueError):
        _resource(name="capped_fields", max_groups=-1)


@strawberry.type
class TallyRow:
    id: str
    count: int


def test_a_column_named_like_the_count_measure_fails_at_build():
    with pytest.raises(AggregateError, match="count"):
        hasura_run_query_resource(
            TallyRow,
            name="tallies",
            filterable=["id"],
            sortable=[],
            source=InMemoryRowSource(lambda info: []),
            groupable=["count"],
        )


def test_aggregate_name_prefixes_the_group_types():
    resource = _resource(name="named_fields", aggregate_name="FieldStat")

    assert _group_type_names(resource) == [
        "named_fields_group",
        "FieldStatGroupKey",
        "FieldStatGroupBySpec",
        "FieldStatGroupableField",
        "FieldStatHaving",
        "FieldStatGroupOrder",
    ]
    data = _run(
        """
        query {
          named_fields_groups(group_by: [{ field: ADDON }]) {
            key { addon }
            aggregate { count }
          }
        }
        """,
        resource=resource,
    )
    assert [g["aggregate"]["count"] for g in data["named_fields_groups"]] == [
        3,
        2,
    ]


# --- model path: inputs validate before the scope callbacks run ------------


def test_model_groups_validate_inputs_before_scoping(db):
    calls: list[str] = []

    @strawberry_django.type(NoteModel)
    class ScopedNote:
        id: auto

    def scoped(info):
        calls.append("get_queryset")
        return NoteModel.objects.all()

    resource = hasura_resource(
        ScopedNote,
        model=NoteModel,
        name="scoped_notes",
        filterable=["id"],
        sortable=["id"],
        aggregatable=[],
        groupable=["status"],
        get_queryset=scoped,
        write_backend=NoteWriteBackend(),
        insert=False,
        update=False,
        delete=False,
    )
    schema = _schema(resource)

    bad = schema.execute_sync(
        "{ scoped_notes_groups(group_by: [{ field: STATUS }], "
        'order_by: [{ field: "title" }]) { aggregate { count } } }'
    )
    good = schema.execute_sync(
        "{ scoped_notes_groups(group_by: [{ field: STATUS }]) "
        "{ aggregate { count } } }"
    )

    assert bad.errors is not None
    assert "Order term" in str(bad.errors[0])
    assert good.errors is None, good.errors
    assert calls == ["get_queryset"]
