"""ORM joins, selection metadata and paging retain resource boundaries."""

from __future__ import annotations

import re
from typing import Any

import pytest
import strawberry
import strawberry_django
from django.db.models import Value

from strawberry_django_hasura import hasura_resource
from tests.models import ReadBoundaryModel, TagModel


@strawberry_django.type(ReadBoundaryModel)
class ReadBoundaryNode:
    score: strawberry.auto
    status: strawberry.auto
    optional_title: strawberry.auto

    @strawberry.field
    def id(self) -> strawberry.ID:
        return strawberry.ID(self.pk)


def read_schema(**kwargs: Any) -> strawberry.Schema:
    resource = hasura_resource(
        ReadBoundaryNode,
        model=ReadBoundaryModel,
        name="read_rows",
        filterable=["id", "tags", "score", "optional_title"],
        sortable=kwargs.pop("sortable", ["id", "score"]),
        aggregatable=["score"],
        groupable=["status"],
        get_queryset=lambda info: ReadBoundaryModel.objects.filter(
            status="public"
        ).annotate(scoped_marker=Value(1)),
        write_backend=None,
        insert=False,
        update=False,
        delete=False,
        **kwargs,
    )
    return strawberry.Schema(query=resource.query, types=resource.types)


@pytest.mark.django_db
def test_many_to_many_membership_preserves_rows_measures_and_scope():
    a = TagModel.objects.create(name="A")
    b = TagModel.objects.create(name="B")
    first = ReadBoundaryModel.objects.create(code="a", score=10)
    second = ReadBoundaryModel.objects.create(code="b", score=20)
    hidden = ReadBoundaryModel.objects.create(
        code="hidden", score=999, status="private"
    )
    first.tags.add(a, b)
    second.tags.add(a)
    hidden.tags.add(a, b)
    schema = read_schema()
    result = schema.execute_sync(
        """query($where: read_rows_bool_exp) {
          read_rows(where: $where, order_by: [{id: asc}]) { id }
          page: read_rows(where: $where, order_by: [{id: asc}],
                          limit: 1, offset: 1) { id }
          read_rows_aggregate(where: $where) {
            aggregate { count sum { score } avg { score } }
            nodes { id }
          }
          read_rows_groups(where: $where, group_by: [{field: STATUS}]) {
            aggregate { count sum { score } }
          }
        }""",
        variable_values={"where": {"tags": {"_in": [str(a.pk), str(b.pk)]}}},
    )
    assert result.errors is None, result.errors
    assert result.data["read_rows"] == [{"id": "a"}, {"id": "b"}]
    assert result.data["page"] == [{"id": "b"}]
    aggregate = result.data["read_rows_aggregate"]
    assert sorted(row["id"] for row in aggregate["nodes"]) == ["a", "b"]
    assert aggregate["aggregate"] == {
        "count": 2,
        "sum": {"score": "30"},
        "avg": {"score": 15.0},
    }
    assert result.data["read_rows_groups"] == [
        {"aggregate": {"count": 2, "sum": {"score": "30"}}}
    ]


@pytest.mark.django_db
@pytest.mark.parametrize("fragment", [False, True])
def test_aggregate_meta_fields_are_not_orm_measure_names(fragment):
    ReadBoundaryModel.objects.create(code="a", score=10)
    schema = read_schema()
    # An inline fragment still passes through the shared selection walker.
    measure = "__typename score"
    if fragment:
        sum_type = re.search(r"sum: (\w+)", schema.as_str())
        assert sum_type is not None
        measure = f"... on {sum_type[1]} {{ __typename score }}"
    result = schema.execute_sync(
        "{ read_rows_aggregate { aggregate { sum { "
        + measure
        + " } } } read_rows_groups(group_by:[{field:STATUS}]) "
        + "{ aggregate { sum { "
        + measure
        + " } } } }"
    )
    assert result.errors is None, result.errors
    assert (
        result.data["read_rows_aggregate"]["aggregate"]["sum"]["score"] == "10"
    )
    assert (
        result.data["read_rows_groups"][0]["aggregate"]["sum"]["score"] == "10"
    )


@pytest.mark.django_db
def test_order_by_public_id_maps_to_custom_primary_key():
    ReadBoundaryModel.objects.create(code="a")
    ReadBoundaryModel.objects.create(code="z")
    result = read_schema().execute_sync(
        "{read_rows(order_by:[{id:desc}]) {id}}"
    )
    assert result.errors is None, result.errors
    assert result.data["read_rows"] == [{"id": "z"}, {"id": "a"}]


@pytest.mark.parametrize("field", ["missing", "score__bad", "tags__name"])
def test_invalid_or_to_many_sortable_path_fails_at_construction(field):
    with pytest.raises(ValueError, match="[Ss]ortable"):
        read_schema(sortable=[field])


@pytest.mark.django_db
@pytest.mark.parametrize("maximum", [0, 1])
def test_row_cap_covers_lists_and_nodes_without_changing_total(maximum):
    ReadBoundaryModel.objects.bulk_create(
        [ReadBoundaryModel(code=str(i), score=i) for i in range(3)]
    )
    result = read_schema(max_rows=maximum).execute_sync(
        """{
          read_rows {id}
          explicit: read_rows(limit: 999) {id}
          read_rows_aggregate {nodes {id} aggregate {count}}
        }"""
    )
    assert result.errors is None, result.errors
    assert len(result.data["read_rows"]) == maximum
    assert len(result.data["explicit"]) == maximum
    assert len(result.data["read_rows_aggregate"]["nodes"]) == maximum
    assert result.data["read_rows_aggregate"]["aggregate"]["count"] == 3


@pytest.mark.django_db
@pytest.mark.parametrize("argument", ["limit", "offset"])
@pytest.mark.parametrize("root", ["read_rows", "read_rows_groups"])
def test_negative_pages_fail_before_queryset_evaluation(argument, root):
    group = "group_by:[{field:STATUS}]," if root.endswith("groups") else ""
    selection = "aggregate{count}" if group else "id"
    result = read_schema().execute_sync(
        f"{{{root}({group}{argument}:-1){{{selection}}}}}"
    )
    assert result.errors
    assert result.errors[0].message == (
        f"{argument} must be greater than or equal to zero"
    )


@pytest.mark.django_db
@pytest.mark.parametrize("operator", ["_eq", "_neq", "_ilike", "_is_null"])
def test_explicit_null_filter_operands_fail_loudly(operator):
    ReadBoundaryModel.objects.create(code="null", optional_title=None)
    result = read_schema().execute_sync(
        f"{{read_rows(where:{{optional_title:{{{operator}:null}}}}){{id}}}}"
    )
    assert result.errors
    assert "does not accept null" in result.errors[0].message
