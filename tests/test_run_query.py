"""The run_query (non-model) resource — Hasura surface over a ``RowSource``.

No Django model and no database: a plain strawberry node + an
``InMemoryRowSource`` over a list of row objects. Proves the same list /
aggregate(count) / by-pk SDL and that filter / order / paginate / count run
through the in-memory dialect evaluator.
"""

from __future__ import annotations

import dataclasses
import datetime
import decimal
from typing import Any

import pytest
import strawberry
from strawberry.types import get_object_definition

from strawberry_django_hasura import (
    InMemoryRowSource,
    apply_in_memory,
    hasura_run_query_resource,
)
from strawberry_django_hasura.filtering import PORTABLE_LOOKUPS
from strawberry_django_hasura.run_query import _LOOKUP_OPS


@dataclasses.dataclass
class _AddonRow:
    id: str
    label: str
    model_count: int


@strawberry.type(name="PlatformAddon")
class PlatformAddon:
    id: strawberry.ID
    label: str
    model_count: int


_ROWS = [
    _AddonRow(id="iam", label="IAM", model_count=5),
    _AddonRow(id="storage", label="Storage", model_count=12),
    _AddonRow(id="notes", label="Notes", model_count=3),
]


def _schema() -> strawberry.Schema:
    resource = hasura_run_query_resource(
        PlatformAddon,
        name="platform_addons",
        filterable=["id", "label", "model_count"],
        sortable=["label", "model_count"],
        source=InMemoryRowSource(lambda info: _ROWS),
    )
    return strawberry.Schema(
        query=resource.query, types=[PlatformAddon, *resource.types]
    )


def _graphql_name(strawberry_type: type) -> str:
    definition = get_object_definition(strawberry_type)
    assert definition is not None
    return definition.name


def test_run_query_resource_exposes_role_metadata() -> None:
    resource = hasura_run_query_resource(
        PlatformAddon,
        name="platform_addons",
        filterable=["id", "label", "model_count"],
        sortable=["label", "model_count"],
        source=InMemoryRowSource(lambda info: _ROWS),
    )

    assert resource.node_type is PlatformAddon
    assert _graphql_name(resource.filter_type) == "platform_addons_bool_exp"
    assert _graphql_name(resource.order_by_type) == "platform_addons_order_by"
    assert _graphql_name(resource.aggregate_container_type) == (
        "platform_addons_aggregate"
    )
    assert _graphql_name(resource.aggregate_type) == "PlatformAddonAggregate"
    assert resource.insert_input_type is None
    assert resource.set_input_type is None
    assert resource.pk_columns_input_type is None
    assert resource.group_type is None
    assert resource.group_key_type is None
    assert resource.group_by_spec_type is None
    assert resource.group_order_type is None
    assert resource.having_type is None
    assert resource.list_root == "platform_addons"
    assert resource.aggregate_root == "platform_addons_aggregate"
    assert resource.detail_root == "platform_addons_by_pk"
    assert resource.groups_root is None
    assert resource.enabled_operations == ()


def test_list_filter_sort_and_aggregate_count() -> None:
    result = _schema().execute_sync(
        """
        query {
          platform_addons(
            where: {model_count: {_gt: 4}}
            order_by: [{model_count: asc}]
          ) { id model_count }
          platform_addons_aggregate(where: {model_count: {_gt: 4}}) {
            aggregate { count }
          }
        }
        """
    )
    assert result.errors is None, result.errors
    assert [row["id"] for row in result.data["platform_addons"]] == [
        "iam",
        "storage",
    ]
    count = result.data["platform_addons_aggregate"]["aggregate"]["count"]
    assert count == 2


def test_list_ilike_and_limit_offset() -> None:
    result = _schema().execute_sync(
        """
        query {
          platform_addons(
            where: {label: {_ilike: "s"}}
            order_by: [{label: asc}]
            limit: 1
            offset: 1
          ) { id }
        }
        """
    )
    assert result.errors is None, result.errors
    # "s" (ci): Notes, Storage -> asc [Notes, Storage] -> page 2 = Storage.
    assert [row["id"] for row in result.data["platform_addons"]] == ["storage"]


def test_list_ilike_accepts_refine_contains_wildcard_pattern() -> None:
    result = _schema().execute_sync(
        """
        query {
          platform_addons(
            where: {label: {_ilike: "%s%"}}
            order_by: [{label: asc}]
          ) { id }
        }
        """
    )
    assert result.errors is None, result.errors
    assert [row["id"] for row in result.data["platform_addons"]] == [
        "notes",
        "storage",
    ]


def test_list_ilike_accepts_hasura_prefix_pattern() -> None:
    result = _schema().execute_sync(
        """
        query {
          platform_addons(where: {label: {_ilike: "sto%"}}) { id }
        }
        """
    )
    assert result.errors is None, result.errors
    assert [row["id"] for row in result.data["platform_addons"]] == ["storage"]


def test_by_pk() -> None:
    result = _schema().execute_sync(
        'query { platform_addons_by_pk(id: "storage") { id label } }'
    )
    assert result.errors is None, result.errors
    assert result.data["platform_addons_by_pk"]["label"] == "Storage"


def test_sdl_shape() -> None:
    sdl = str(_schema())
    for marker in (
        "platform_addons(",
        "platform_addons_bool_exp",
        "platform_addons_order_by",
        "platform_addons_aggregate",
        "platform_addons_by_pk",
        "model_count",
    ):
        assert marker in sdl, marker


def test_decimal_maps_to_float_and_nulls_sort_first_on_asc() -> None:
    @dataclasses.dataclass
    class _Thing:
        id: str
        amount: decimal.Decimal
        note: str | None

    @strawberry.type(name="Thing")
    class Thing:
        id: strawberry.ID
        amount: decimal.Decimal
        note: str | None

    rows = [
        _Thing(id="a", amount=decimal.Decimal("2.5"), note=None),
        _Thing(id="b", amount=decimal.Decimal("1.5"), note="x"),
    ]
    resource = hasura_run_query_resource(
        Thing,
        name="things",
        filterable=["id", "amount", "note"],
        sortable=["amount", "note"],
        source=InMemoryRowSource(lambda info: rows),
    )
    schema = strawberry.Schema(
        query=resource.query, types=[Thing, *resource.types]
    )
    # decimal -> the shared owner's Float_comparison_exp, not String.
    assert "Float_comparison_exp" in str(schema)
    # NULL sorts first on asc (matches the model path's SQLite default).
    result = schema.execute_sync("{ things(order_by: [{note: asc}]) { id } }")
    assert result.errors is None, result.errors
    assert [row["id"] for row in result.data["things"]] == ["a", "b"]


def test_empty_not_matches_all_and_null_operand_is_noop() -> None:
    schema = _schema()
    empty_not = schema.execute_sync(
        "{ platform_addons(where: {_not: {}}) { id } }"
    )
    assert empty_not.errors is None, empty_not.errors
    assert len(empty_not.data["platform_addons"]) == 3  # ~Q() matches all
    null_operand = schema.execute_sync(
        "{ platform_addons(where: {model_count: {_gt: null}}) { id } }"
    )
    assert null_operand.errors is None, null_operand.errors  # no crash, no-op
    assert len(null_operand.data["platform_addons"]) == 3


def test_camelcase_node_field_is_filtered_by_python_attr() -> None:
    # A node field with an explicit camelCase wire name: filter/sort must read
    # the python attribute off the row, not the (absent) wire attribute.
    @dataclasses.dataclass
    class _WidgetRow:
        id: str
        model_count: int

    @strawberry.type(name="Widget")
    class Widget:
        id: strawberry.ID
        model_count: int = strawberry.field(name="modelCount")

    rows = [_WidgetRow(id="w1", model_count=5)]
    resource = hasura_run_query_resource(
        Widget,
        name="widgets",
        filterable=["id", "model_count"],
        sortable=["model_count"],
        source=InMemoryRowSource(lambda info: rows),
    )
    schema = strawberry.Schema(
        query=resource.query, types=[Widget, *resource.types]
    )
    result = schema.execute_sync(
        "{ widgets(where: {model_count: {_gt: 4}}) { id } }"
    )
    assert result.errors is None, result.errors
    assert [row["id"] for row in result.data["widgets"]] == ["w1"]


def _int_id_schema() -> strawberry.Schema:
    @dataclasses.dataclass
    class _IntRow:
        id: int
        label: str

    @strawberry.type(name="IntThing")
    class IntThing:
        id: strawberry.ID
        label: str

    rows = [_IntRow(id=1, label="one"), _IntRow(id=2, label="two")]
    resource = hasura_run_query_resource(
        IntThing,
        name="int_things",
        filterable=["id", "label"],
        sortable=["label"],
        source=InMemoryRowSource(lambda info: rows),
    )
    return strawberry.Schema(
        query=resource.query, types=[IntThing, *resource.types]
    )


def test_int_id_by_pk_and_eq_filter_agree() -> None:
    # ``by_pk`` and the list's ``id { _eq }`` must return the same row for a
    # non-string (int) row id — both coerce the id surface to text.
    schema = _int_id_schema()
    by_pk = schema.execute_sync('{ int_things_by_pk(id: "1") { label } }')
    assert by_pk.errors is None, by_pk.errors
    assert by_pk.data["int_things_by_pk"]["label"] == "one"

    eq = schema.execute_sync(
        '{ int_things(where: {id: {_eq: "1"}}) { label } }'
    )
    assert eq.errors is None, eq.errors
    assert [r["label"] for r in eq.data["int_things"]] == ["one"]

    in_ = schema.execute_sync(
        '{ int_things(where: {id: {_in: ["1", "2"]}}) { id } }'
    )
    assert in_.errors is None, in_.errors
    assert [r["id"] for r in in_.data["int_things"]] == ["1", "2"]


def test_cross_type_datetime_comparison_does_not_crash() -> None:
    # DateTimeComparison maps both date and datetime; a date row vs a datetime
    # operand is uncomparable and must exclude the row, not crash the query.
    @dataclasses.dataclass
    class _EventRow:
        id: str
        on: datetime.date

    @strawberry.type(name="Event")
    class Event:
        id: strawberry.ID
        on: datetime.date

    rows = [_EventRow(id="e1", on=datetime.date(2020, 1, 1))]
    resource = hasura_run_query_resource(
        Event,
        name="events",
        filterable=["id", "on"],
        sortable=["on"],
        source=InMemoryRowSource(lambda info: rows),
    )
    schema = strawberry.Schema(
        query=resource.query, types=[Event, *resource.types]
    )
    result = schema.execute_sync(
        '{ events(where: {on: {_gt: "2019-01-01T00:00:00"}}) { id } }'
    )
    assert result.errors is None, result.errors
    assert result.data["events"] == []


def test_empty_or_matches_all() -> None:
    result = _schema().execute_sync(
        "{ platform_addons(where: {_or: []}) { id } }"
    )
    assert result.errors is None, result.errors
    assert len(result.data["platform_addons"]) == 3


def test_unmapped_operator_fails_loud_on_empty_source() -> None:
    # The fail-fast must fire even with zero rows (a per-row check would not).
    resource = hasura_run_query_resource(
        PlatformAddon,
        name="platform_addons",
        filterable=["id", "label", "model_count"],
        sortable=["label", "model_count"],
        source=InMemoryRowSource(lambda info: []),
    )
    schema = strawberry.Schema(
        query=resource.query, types=[PlatformAddon, *resource.types]
    )
    result = schema.execute_sync(
        '{ platform_addons(where: {label: {_iregex: "x"}}) { id } }'
    )
    assert result.errors is not None
    assert "not supported by the in-memory row source" in str(result.errors[0])


def test_id_field_must_be_filterable() -> None:
    with pytest.raises(TypeError, match="id_field"):
        hasura_run_query_resource(
            PlatformAddon,
            name="platform_addons",
            filterable=["label"],
            sortable=["label"],
            source=InMemoryRowSource(lambda info: _ROWS),
        )


def test_by_pk_pushes_id_predicate_down_and_bounds_to_one() -> None:
    # by_pk must hand the source an ``id _eq`` where + ``limit=1`` rather
    # than pulling the whole dataset (``where=None``) and scanning.
    calls: list[dict[str, Any]] = []

    class _RecordingSource:
        def query(
            self,
            info: strawberry.Info,
            *,
            where: Any,
            order_by: Any,
            limit: int | None,
            offset: int | None,
        ) -> list[Any]:
            calls.append({"where": where, "limit": limit})
            return apply_in_memory(
                _ROWS, where, order_by, limit, offset, id_field="id"
            )

        def count(self, info: strawberry.Info, *, where: Any) -> int:
            return len(apply_in_memory(_ROWS, where, None, None, None))

    resource = hasura_run_query_resource(
        PlatformAddon,
        name="platform_addons",
        filterable=["id", "label", "model_count"],
        sortable=["label", "model_count"],
        source=_RecordingSource(),
    )
    schema = strawberry.Schema(
        query=resource.query, types=[PlatformAddon, *resource.types]
    )
    result = schema.execute_sync(
        '{ platform_addons_by_pk(id: "storage") { label } }'
    )
    assert result.errors is None, result.errors
    assert result.data["platform_addons_by_pk"]["label"] == "Storage"
    assert len(calls) == 1
    assert calls[0]["limit"] == 1
    assert calls[0]["where"] is not None
    assert calls[0]["where"].id.eq == "storage"


def test_paging_is_stable_over_unordered_source() -> None:
    # No order_by + a source whose row order differs between requests: the
    # id_field tiebreaker keeps limit/offset pages deterministic.
    page_a = apply_in_memory(_ROWS, None, None, 2, 0, id_field="id")
    page_b = apply_in_memory(
        list(reversed(_ROWS)), None, None, 2, 0, id_field="id"
    )
    assert [r.id for r in page_a] == [r.id for r in page_b]


def test_lookup_ops_mirror_filtering_lookups() -> None:
    assert set(_LOOKUP_OPS) == PORTABLE_LOOKUPS
