"""Declared wire ordering composes scoped, annotated native QuerySets."""

from __future__ import annotations

import re

import pytest
import strawberry
from django.db import connection
from django.db.models import Value
from django.db.models.functions import Coalesce, Lower
from django.test.utils import CaptureQueriesContext

from strawberry_django_hasura import OrderBy, apply_ordering, hasura_resource
from tests.models import ReadBoundaryModel
from tests.test_read_boundaries import ReadBoundaryNode


def alias_resource(*, aliases=None, sortable=None, annotated=True):
    def source(info):
        queryset = ReadBoundaryModel.objects.filter(status="public")
        if annotated:
            queryset = queryset.annotate(
                _sort_title=Lower(Coalesce("optional_title", Value("")))
            )
        return queryset

    return hasura_resource(
        ReadBoundaryNode,
        model=ReadBoundaryModel,
        name="alias_rows",
        filterable=["id", "score"],
        sortable=["id", "score", "title"] if sortable is None else sortable,
        sortable_aliases={"title": "_sort_title"}
        if aliases is None
        else aliases,
        aggregatable=["score"],
        get_queryset=source,
        write_backend=None,
        insert=False,
        update=False,
        delete=False,
    )


def schema_for(resource):
    return strawberry.Schema(query=resource.query, types=resource.types)


@pytest.fixture
def rows(db):
    # Reverse PK insertion makes the tie ordering observable.
    return ReadBoundaryModel.objects.bulk_create(
        [
            ReadBoundaryModel(code="c", optional_title="alpha", score=3),
            ReadBoundaryModel(code="b", optional_title="Beta", score=2),
            ReadBoundaryModel(code="a", optional_title="ALPHA", score=1),
            ReadBoundaryModel(code="n", optional_title=None, score=4),
            ReadBoundaryModel(
                code="hidden",
                optional_title="aardvark",
                status="private",
                score=999,
            ),
        ]
    )


@pytest.mark.parametrize(
    "direction,expected",
    [
        ("asc", ["n", "a", "c", "b"]),
        ("desc", ["b", "a", "c", "n"]),
    ],
)
def test_native_annotation_order_scope_and_page_ties(
    rows, direction, expected
):
    schema = schema_for(alias_resource())
    with CaptureQueriesContext(connection) as queries:
        result = schema.execute_sync(
            "{ alias_rows(order_by:[{title:" + direction + "}]) {id} "
            "page:alias_rows(order_by:[{title:"
            + direction
            + "}],limit:2,offset:1){id} "
            "alias_rows_aggregate {aggregate {count sum {score}}} }"
        )
    assert result.errors is None, result.errors
    assert result.data["alias_rows"] == [{"id": value} for value in expected]
    assert result.data["page"] == [{"id": value} for value in expected[1:3]]
    assert result.data["alias_rows_aggregate"]["aggregate"] == {
        "count": 4,
        "sum": {"score": "10"},
    }
    assert len(queries) == 3  # One list, one page, native free aggregate.
    page_sql = queries[1]["sql"]
    assert "LOWER(COALESCE(" in page_sql
    assert re.search(
        r"ORDER BY .+ "
        + direction.upper()
        + r', "tests_readboundarymodel"\."code" ASC',
        page_sql,
    )
    assert "LIMIT 2 OFFSET 1" in page_sql
    assert "\"status\" = 'public'" in page_sql


@pytest.mark.django_db
def test_alias_annotation_is_required_only_when_selected():
    schema = schema_for(alias_resource(annotated=False))
    result = schema.execute_sync("{alias_rows(order_by:[{score:asc}]){id}}")
    assert result.errors is None, result.errors
    with CaptureQueriesContext(connection) as queries:
        result = schema.execute_sync(
            "{alias_rows(order_by:[{title:asc}]){id}}"
        )
    assert result.errors
    assert result.errors[0].message == (
        "Sortable alias 'title' requires queryset annotation '_sort_title'"
    )
    assert len(queries) == 0


@pytest.mark.parametrize(
    "sortable,aliases",
    [
        (["title"], {"missing": "_sort_title"}),
        (["id"], {"id": "_sort_title"}),
        (["score"], {"score": "_sort_title"}),
        (["title"], {"title": "score"}),
        (["title"], {"title": "pk"}),
        (["title"], {"title": "-sort_title"}),
        (["title"], {"title": "nested__title"}),
        (["1title"], {"1title": "_sort_title"}),
        (["__title"], {"__title": "_sort_title"}),
        (["some-title"], {"some-title": "_sort_title"}),
    ],
)
def test_invalid_alias_declarations_fail_at_construction(sortable, aliases):
    with pytest.raises(ValueError, match="Sortable alias"):
        alias_resource(sortable=sortable, aliases=aliases)


def test_alias_mapping_is_copied_and_wire_input_is_allowlisted(rows):
    aliases = {"title": "_sort_title"}
    schema = schema_for(alias_resource(aliases=aliases))
    aliases["title"] = "missing_after_build"
    result = schema.execute_sync("{alias_rows(order_by:[{title:asc}]){id}}")
    assert result.errors is None, result.errors
    result = schema.execute_sync(
        "{alias_rows(order_by:[{_sort_title:asc}]){id}}"
    )
    assert (
        result.errors
        and "not defined by type 'alias_rows_order_by'"
        in result.errors[0].message
    )
    sdl = schema.as_str()
    order_input = re.search(r"input alias_rows_order_by \{([^}]+)\}", sdl)[1]
    assert "title: order_by" in order_input
    assert "_sort_title:" not in order_input
    # This feature does not add ordering/paging arguments to aggregate nodes.
    assert re.search(
        r"alias_rows_aggregate\(where: alias_rows_bool_exp(?: = null)?\)", sdl
    )


def test_native_pk_sort_remains_the_selected_tiebreaker(rows):
    resource = alias_resource()
    queryset = ReadBoundaryModel.objects.filter(status="public")
    ordered = apply_ordering(
        queryset,
        [resource.order_by_type(score=OrderBy.asc, id=OrderBy.desc)],
        id_column="code",
    )
    assert ordered.query.order_by == ("-code", "score")
    assert list(ordered.values_list("pk", flat=True)) == ["n", "c", "b", "a"]
    assert apply_ordering(
        queryset.order_by("-score"), None
    ).query.order_by == ("-score",)
