"""Runtime grouped aggregate companion root (PREVIEW / NDC-shaped).

``<res>_groups`` pairs the typed upstream ``<Model>GroupKey`` with the FREE
``<Model>Aggregate`` (no reshape), composing ``strawberry-django-aggregates``'
public surface. NDC semantics: ``group_by`` dimensions + ``where`` (pre-group)
+ ``having`` (over aggregates) + ``order_by`` + offset paging.
"""

from __future__ import annotations


def test_groups_pair_typed_key_with_free_aggregate(schema, seeded_notes):
    result = schema.execute_sync(
        """
        query {
          notes_groups(group_by: [{ field: STATUS }]) {
            key { status }
            aggregate { count  sum { word_count } }
          }
        }
        """
    )
    assert result.errors is None, result.errors
    by_status = {
        group["key"]["status"]: group["aggregate"]
        for group in result.data["notes_groups"]
    }
    assert by_status["published"]["count"] == 2
    assert int(by_status["published"]["sum"]["word_count"]) == 30
    assert by_status["draft"]["count"] == 1
    assert int(by_status["draft"]["sum"]["word_count"]) == 30


def test_groups_apply_hasura_where_before_aggregating(schema, seeded_notes):
    result = schema.execute_sync(
        """
        query($w: notes_bool_exp) {
          notes_groups(group_by: [{ field: STATUS }], where: $w) {
            key { status }
            aggregate { count }
          }
        }
        """,
        variable_values={"w": {"status": {"_eq": "published"}}},
    )
    assert result.errors is None, result.errors
    groups = result.data["notes_groups"]
    assert len(groups) == 1
    assert groups[0]["key"]["status"] == "published"
    assert groups[0]["aggregate"]["count"] == 2


def test_groups_support_date_granularity_and_bucket_range(
    schema, seeded_notes
):
    result = schema.execute_sync(
        """
        query {
          notes_groups(
            group_by: [{ field: UPDATED_AT, granularity: MONTH }]
          ) {
            key { updated_at_month  updated_at_month_range { from to } }
            aggregate { count }
          }
        }
        """
    )
    assert result.errors is None, result.errors
    groups = result.data["notes_groups"]
    assert len(groups) == 1
    key = groups[0]["key"]
    # The typed key carries the bucketed value AND its half-open range
    # sibling — the richness the old stringly-typed shape threw away.
    assert key["updated_at_month"]
    assert key["updated_at_month_range"]["from"] <= key["updated_at_month"]
    assert key["updated_at_month_range"]["to"] > key["updated_at_month"]
    assert groups[0]["aggregate"]["count"] == 3


def test_groups_having_filters_on_an_aggregate(schema, seeded_notes):
    result = schema.execute_sync(
        """
        query {
          notes_groups(
            group_by: [{ field: STATUS }], having: { count_gt: 1 }
          ) {
            key { status }
            aggregate { count }
          }
        }
        """
    )
    assert result.errors is None, result.errors
    groups = result.data["notes_groups"]
    # published (count 2) survives count_gt: 1; draft (count 1) is filtered.
    assert len(groups) == 1
    assert groups[0]["key"]["status"] == "published"
    assert groups[0]["aggregate"]["count"] == 2


def test_groups_order_by_and_offset_page(schema, seeded_notes):
    query = """
        query($limit: Int, $offset: Int) {
          notes_groups(
            group_by: [{ field: STATUS }],
            order_by: [{ field: "status" }],
            limit: $limit, offset: $offset
          ) { key { status } }
        }
    """
    page0 = schema.execute_sync(
        query, variable_values={"limit": 1, "offset": 0}
    )
    page1 = schema.execute_sync(
        query, variable_values={"limit": 1, "offset": 1}
    )
    assert page0.errors is None, page0.errors
    assert page1.errors is None, page1.errors
    # order_by makes offset paging deterministic: draft < published (asc).
    assert page0.data["notes_groups"][0]["key"]["status"] == "draft"
    assert page1.data["notes_groups"][0]["key"]["status"] == "published"
