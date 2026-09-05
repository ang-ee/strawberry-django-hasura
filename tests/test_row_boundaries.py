"""Scoped row sources and scalar behavior through the generated GraphQL API."""

from __future__ import annotations

import datetime
import uuid
from dataclasses import dataclass
from enum import Enum
from types import SimpleNamespace
from typing import Any

import pytest
import strawberry
from strawberry.scalars import JSON

from strawberry_django_hasura import (
    InMemoryRowSource,
    apply_in_memory,
    hasura_run_query_resource,
)


@strawberry.enum
class BoundaryState(Enum):
    ACTIVE = "active"
    ARCHIVED = "archived"


@dataclass
class BoundaryRow:
    id: str
    label: str = "keep"
    state: BoundaryState = BoundaryState.ACTIVE
    on: datetime.date = datetime.date(2026, 1, 1)
    at: datetime.time = datetime.time(12)
    payload: Any = None


@strawberry.type
class BoundaryNode:
    id: strawberry.ID
    label: str
    state: BoundaryState
    on: datetime.date
    at: datetime.time
    payload: JSON | None


def row_schema(rows: list[BoundaryRow], **kwargs: Any) -> strawberry.Schema:
    resource = hasura_run_query_resource(
        BoundaryNode,
        name="boundary_rows",
        filterable=["id", "label", "state", "on", "at", "payload"],
        sortable=["id", "state", "on", "at"],
        source=kwargs.pop("source", InMemoryRowSource(lambda info: rows)),
        **kwargs,
    )
    return strawberry.Schema(query=resource.query, types=resource.types)


@pytest.mark.parametrize("object_context", [False, True])
def test_reused_context_obtains_current_scoped_rows(object_context: bool):
    context = SimpleNamespace(actor="a") if object_context else {"actor": "a"}
    calls = []

    def get_rows(info: strawberry.Info):
        actor = info.context.actor if object_context else info.context["actor"]
        calls.append(actor)
        return [BoundaryRow(actor)]

    schema = row_schema([], source=InMemoryRowSource(get_rows))
    query = """{
      boundary_rows { id }
      boundary_rows_aggregate { aggregate { count } nodes { id } }
    }"""
    first = schema.execute_sync(query, context_value=context)
    assert first.errors is None
    if object_context:
        context.actor = "b"
    else:
        context["actor"] = "b"
    second = schema.execute_sync(query, context_value=context)
    assert second.errors is None
    assert first.data["boundary_rows"] == [{"id": "a"}]
    assert second.data["boundary_rows"] == [{"id": "b"}]
    assert second.data["boundary_rows_aggregate"] == {
        "aggregate": {"count": 1},
        "nodes": [{"id": "b"}],
    }
    assert calls == ["a", "a", "a", "b", "b", "b"]


def test_enum_values_filter_and_sort_as_the_declared_strings():
    schema = row_schema(
        [BoundaryRow("a"), BoundaryRow("b", state=BoundaryState.ARCHIVED)]
    )
    result = schema.execute_sync(
        """{
          exact: boundary_rows(where: {state: {_eq: "active"}}) { id }
          member: boundary_rows(where: {state: {_in: ["archived"]}}) { id }
          sorted: boundary_rows(order_by: [{state: desc}]) { id state }
        }"""
    )
    assert result.errors is None
    assert result.data == {
        "exact": [{"id": "a"}],
        "member": [{"id": "b"}],
        "sorted": [
            {"id": "b", "state": "ARCHIVED"},
            {"id": "a", "state": "ACTIVE"},
        ],
    }


def test_uuid_field_matches_string_equality_and_membership_operands():
    @strawberry.type
    class UUIDNode:
        id: strawberry.ID
        token: uuid.UUID

    token = uuid.UUID("12345678-1234-5678-1234-567812345678")
    rows = [UUIDNode(id="a", token=token)]
    resource = hasura_run_query_resource(
        UUIDNode,
        name="uuid_rows",
        filterable=["id", "token"],
        sortable=["token"],
        source=InMemoryRowSource(lambda info: rows),
    )
    schema = strawberry.Schema(query=resource.query, types=resource.types)
    result = schema.execute_sync(
        """query($token: String!) {
          exact: uuid_rows(where: {token: {_eq: $token}}) { id }
          member: uuid_rows(where: {token: {_in: [$token]}}) { id }
        }""",
        variable_values={"token": str(token)},
    )
    assert result.errors is None
    assert result.data == {"exact": [{"id": "a"}], "member": [{"id": "a"}]}


def test_date_and_time_wire_values_round_trip_in_filters():
    schema = row_schema([BoundaryRow("a")])
    result = schema.execute_sync(
        """{ boundary_rows(where: {
          on: {_eq: "2026-01-01"}, at: {_gte: "12:00:00"}
        }) { id on at } }"""
    )
    assert result.errors is None
    assert result.data["boundary_rows"] == [
        {"id": "a", "on": "2026-01-01", "at": "12:00:00"}
    ]
    sdl = schema.as_str()
    assert "on: Date_comparison_exp" in sdl
    assert "at: Time_comparison_exp" in sdl


@pytest.mark.parametrize(
    ("where", "ids"),
    [
        ({"_or": [{}, {"label": {"_eq": "keep"}}]}, ["a"]),
        ({"_not": {"label": {}}}, ["a", "b"]),
        ({"_not": {"_or": [{}, {"label": {"_eq": "keep"}}]}}, ["b"]),
        ({"_not": {"_and": [{}, {"label": {}}]}}, ["a", "b"]),
        ({"_and": [{}, {"label": {"_eq": "keep"}}]}, ["a"]),
    ],
)
def test_empty_boolean_branches_follow_django_q_semantics(where, ids):
    result = row_schema(
        [BoundaryRow("a"), BoundaryRow("b", label="other")]
    ).execute_sync(
        "query($w: boundary_rows_bool_exp) {boundary_rows(where: $w) {id}}",
        variable_values={"w": where},
    )
    assert result.errors is None
    assert result.data["boundary_rows"] == [{"id": value} for value in ids]


@pytest.mark.parametrize("operator", ["_eq", "_gt", "_in", "_is_null"])
def test_null_comparison_rejected_even_without_rows(operator):
    result = row_schema([]).execute_sync(
        "query($w: boundary_rows_bool_exp) {boundary_rows(where: $w) {id}}",
        variable_values={"w": {"label": {operator: None}}},
    )
    assert result.errors is not None
    assert "does not accept null" in result.errors[0].message


@pytest.mark.parametrize(
    ("value", "operand", "matches"),
    [
        ({}, {"missing": None}, False),
        ({"present": None}, {"present": None}, True),
        ({"a": {"x": 1, "y": 2}}, {"a": {"x": 1}}, True),
        ({"a": {"x": 1}}, {"x": 1}, False),
        ({"a": {"x": 1}}, {"a": {}}, True),
        ({"a": [1]}, {"a": 1}, False),
        ([1, 2, 3], [3, 1, 1], True),
        ([1, 2, [1, 3]], [1, 3], False),
        ([1, 2, [1, 3]], [[1, 3]], True),
        ([{"a": 1, "b": 2}], [{"a": 1}], True),
        (["foo", "bar"], "bar", True),
        ("bar", ["bar"], False),
        ("foobar", "foo", False),
        ("foo", "foo", True),
        (True, 1, False),
        ([True], [1], False),
        ({"a": True}, {"a": 1}, False),
        (1, 1.0, True),
    ],
)
def test_json_contains_matches_jsonb_structural_rules(value, operand, matches):
    result = row_schema([BoundaryRow("a", payload=value)]).execute_sync(
        "query($w: boundary_rows_bool_exp) {boundary_rows(where: $w) {id}}",
        variable_values={"w": {"payload": {"_contains": operand}}},
    )
    assert result.errors is None
    assert result.data["boundary_rows"] == ([{"id": "a"}] if matches else [])


def test_json_equality_distinguishes_booleans_and_array_order():
    schema = row_schema(
        [
            BoundaryRow("bool", payload={"a": True}),
            BoundaryRow("number", payload={"a": 1}),
            BoundaryRow("array", payload=[1, 2]),
        ]
    )
    result = schema.execute_sync(
        """query($equal: JSON!, $ordered: JSON!) {
          scalar: boundary_rows(where: {payload: {_eq: $equal}}) { id }
          array: boundary_rows(where: {payload: {_eq: $ordered}}) { id }
        }""",
        variable_values={"equal": {"a": 1}, "ordered": [2, 1]},
    )
    assert result.errors is None
    assert result.data == {"scalar": [{"id": "number"}], "array": []}


def test_row_caps_bound_lists_and_nodes_but_keep_exact_count():
    result = row_schema(
        [BoundaryRow("a"), BoundaryRow("b"), BoundaryRow("c")], max_rows=2
    ).execute_sync(
        """{
          default_page: boundary_rows { id }
          oversized: boundary_rows(limit: 99) { id }
          boundary_rows_aggregate { aggregate { count } nodes { id } }
        }"""
    )
    assert result.errors is None
    assert result.data == {
        "default_page": [{"id": "a"}, {"id": "b"}],
        "oversized": [{"id": "a"}, {"id": "b"}],
        "boundary_rows_aggregate": {
            "aggregate": {"count": 3},
            "nodes": [{"id": "a"}, {"id": "b"}],
        },
    }


@pytest.mark.parametrize(("limit", "offset"), [(-1, None), (None, -1)])
def test_negative_paging_rejected_before_calling_a_delegated_source(
    limit, offset
):
    class Source:
        def query(self, *args, **kwargs):
            raise AssertionError("source must not receive invalid pagination")

        def count(self, *args, **kwargs):
            return 0

    result = row_schema([], source=Source()).execute_sync(
        """query($limit: Int, $offset: Int) {
          boundary_rows(limit: $limit, offset: $offset) { id }
        }""",
        variable_values={"limit": limit, "offset": offset},
    )
    assert result.errors is not None
    assert "greater than or equal to zero" in result.errors[0].message
    with pytest.raises(ValueError, match="greater than or equal to zero"):
        apply_in_memory([], None, None, limit, offset)


def test_invalid_cap_fails_at_construction_and_legacy_name_is_available():
    with pytest.raises(ValueError, match="greater than or equal to zero"):
        row_schema([], max_rows=-1)
    assert (
        "type BoundaryAggregate"
        in row_schema([], aggregate_name="Boundary").as_str()
    )
