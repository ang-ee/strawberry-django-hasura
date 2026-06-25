"""The run_query (non-model) resource — Hasura surface over a ``RowSource``.

No Django model and no database: a plain strawberry node + an
``InMemoryRowSource`` over a list of row objects. Proves the same list /
aggregate(count) / by-pk SDL and that filter / order / paginate / count run
through the in-memory dialect evaluator.
"""

from __future__ import annotations

import dataclasses
import decimal

import strawberry

from strawberry_django_hasura import (
    InMemoryRowSource,
    hasura_run_query_resource,
)


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
