"""Runtime aggregate — the FREE ``<resource>_aggregate`` container.

Converted from the spike's aggregate checks: the ``aggregate`` field is the
native ``<Model>Aggregate`` type from ``strawberry-django-aggregates`` (zero
reshape), filled by composing ``compute_aggregation`` +
``shape_aggregate_row``. Asserts count/sum/avg/min/max come straight from the
library type, the ``where`` filter narrows before aggregating, and ``nodes``
returns the same rows.
"""

from __future__ import annotations

import strawberry

from strawberry_django_hasura import hasura_resource
from tests.demo_schema import Note, NoteWriteBackend, decode_sqid

AGG_QUERY = """query($w: notes_bool_exp){
  notes_aggregate(where:$w){
    aggregate {
      count
      sum { word_count }
      avg { word_count }
      min { word_count }
      max { word_count }
    }
    nodes { id title }
  }
}"""


def test_aggregate_unfiltered(schema, seeded_notes):
    result = schema.execute_sync(AGG_QUERY, variable_values={"w": None})
    assert result.errors is None, result.errors
    agg = result.data["notes_aggregate"]["aggregate"]
    # All three: Alpha(10) + Bravo(30) + Cee(20) = 60.
    # SUM over an IntegerField is the library's BigInt scalar (a JSON string).
    assert agg["count"] == 3
    assert int(agg["sum"]["word_count"]) == 60
    assert agg["avg"]["word_count"] == 20
    assert agg["min"]["word_count"] == 10
    assert agg["max"]["word_count"] == 30


def test_aggregate_filtered_by_where(schema, seeded_notes):
    """The ``where`` narrows the rows before aggregating (the spike's
    published-only aggregate check)."""
    result = schema.execute_sync(
        AGG_QUERY, variable_values={"w": {"status": {"_eq": "published"}}}
    )
    assert result.errors is None, result.errors
    container = result.data["notes_aggregate"]
    agg = container["aggregate"]
    # published = Alpha(10) + Cee(20): count 2, sum 30, avg 15, min 10, max 20.
    assert agg["count"] == 2
    assert int(agg["sum"]["word_count"]) == 30
    assert agg["avg"]["word_count"] == 15
    assert agg["min"]["word_count"] == 10
    assert agg["max"]["word_count"] == 20
    # nodes returns the SAME filtered rows.
    assert {n["title"] for n in container["nodes"]} == {"Alpha", "Cee"}


def test_aggregate_count_only_is_selection_driven(schema, seeded_notes):
    """Selecting only ``count`` computes only the count (the provider's
    getList total path issues ``aggregate { count }``)."""
    result = schema.execute_sync(
        """query($w: notes_bool_exp){
             notes_aggregate(where:$w){ aggregate { count } } }""",
        variable_values={"w": {"status": {"_eq": "published"}}},
    )
    assert result.errors is None, result.errors
    assert result.data["notes_aggregate"]["aggregate"]["count"] == 2


def test_aggregate_sum_is_bigint_string(schema, seeded_notes):
    """SUM over an IntegerField is the library's ``BigInt`` scalar (a
    JSON string on the wire) — proof the value comes from the native type,
    not a reshaped Float."""
    result = schema.execute_sync(
        "{ notes_aggregate { aggregate { sum { word_count } } } }"
    )
    assert result.errors is None, result.errors
    raw = result.data["notes_aggregate"]["aggregate"]["sum"]["word_count"]
    # BigInt serializes as a string; the value is still 60.
    assert int(raw) == 60


def test_aggregate_queryset_can_differ_from_nodes_queryset(seeded_notes):
    """Aggregate math and aggregate ``nodes`` may need different policies."""

    def read_queryset(_info):
        return seeded_notes.objects.filter(status="published")

    def aggregate_queryset(_info):
        return seeded_notes.objects.filter(title="Alpha")

    resource = hasura_resource(
        Note,
        model=seeded_notes,
        name="policy_notes",
        filterable=["id", "title", "word_count", "status"],
        sortable=["title"],
        aggregatable=["word_count"],
        get_queryset=read_queryset,
        get_aggregate_queryset=aggregate_queryset,
        write_backend=NoteWriteBackend(),
        id_decode=decode_sqid,
    )
    policy_schema = strawberry.Schema(
        query=resource.query,
        mutation=resource.mutation,
        types=resource.types,
    )

    result = policy_schema.execute_sync(
        """{
          policy_notes_aggregate {
            aggregate { count sum { word_count } }
            nodes { title }
          }
        }"""
    )

    assert result.errors is None, result.errors
    container = result.data["policy_notes_aggregate"]
    assert container["aggregate"]["count"] == 1
    assert int(container["aggregate"]["sum"]["word_count"]) == 10
    assert {row["title"] for row in container["nodes"]} == {"Alpha", "Cee"}
