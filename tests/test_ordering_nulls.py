"""Hasura order_by null placement is independent of the database default."""

from __future__ import annotations

import pytest
import strawberry
import strawberry_django
from django.db.models import F
from django.db.models.functions import Lower

from strawberry_django_hasura import (
    OrderBy,
    SortAlias,
    apply_ordering,
    hasura_resource,
    order_clauses,
)
from tests.models import AuthorModel, AuthorProfileModel, ReadBoundaryModel
from tests.test_read_boundaries import ReadBoundaryNode


@strawberry_django.type(AuthorProfileModel)
class ProfileNode:
    label: strawberry.auto

    @strawberry.field
    def id(self) -> strawberry.ID:
        return strawberry.ID(self.pk)


MEMBERS = [
    ("asc", ["a1", "a2", "b", "n"]),
    ("asc_nulls_first", ["n", "a1", "a2", "b"]),
    ("asc_nulls_last", ["a1", "a2", "b", "n"]),
    ("desc", ["n", "b", "a1", "a2"]),
    ("desc_nulls_first", ["n", "b", "a1", "a2"]),
    ("desc_nulls_last", ["b", "a1", "a2", "n"]),
]


@pytest.fixture
def nullable_rows(db):
    # Reverse tie insertion makes the appended PK order observable.
    ReadBoundaryModel.objects.bulk_create(
        [
            ReadBoundaryModel(code="a2", optional_title="a", score=1),
            ReadBoundaryModel(code="n", optional_title=None, score=4),
            ReadBoundaryModel(code="b", optional_title="b", score=3),
            ReadBoundaryModel(code="a1", optional_title="a", score=2),
        ]
    )


def _row_schema(*, alias: str | None = None) -> strawberry.Schema:
    def source(info):
        queryset = ReadBoundaryModel.objects.all()
        if alias == "plain":
            queryset = queryset.annotate(_title=Lower("optional_title"))
        return queryset

    aliases = None
    if alias == "plain":
        aliases = {"title": "_title"}
    elif alias == "lazy":
        aliases = {
            "title": SortAlias(
                "_title", lambda info, qs: Lower("optional_title")
            )
        }
    resource = hasura_resource(
        ReadBoundaryNode,
        model=ReadBoundaryModel,
        name="null_rows",
        filterable=["id"],
        sortable=["id", "title"]
        if alias
        else ["id", "optional_title", "score"],
        sortable_aliases=aliases,
        aggregatable=[],
        get_queryset=source,
        write_backend=None,
        insert=False,
        update=False,
        delete=False,
    )
    return strawberry.Schema(query=resource.query, types=resource.types)


@pytest.mark.parametrize("member,expected", MEMBERS)
@pytest.mark.parametrize("alias", [None, "plain", "lazy"])
def test_nullable_native_and_alias_order_on_each_backend(
    nullable_rows, alias, member, expected
):
    field = "title" if alias else "optional_title"
    result = _row_schema(alias=alias).execute_sync(
        "{ null_rows(order_by: [{" + field + ": " + member + "}]) { id } }"
    )
    assert result.errors is None, result.errors
    assert [row["id"] for row in result.data["null_rows"]] == expected


@pytest.mark.django_db
@pytest.mark.parametrize("member,expected", MEMBERS)
def test_nullable_to_one_path_on_each_backend(member, expected):
    a1 = AuthorModel.objects.create(name="a")
    a2 = AuthorModel.objects.create(name="a")
    b = AuthorModel.objects.create(name="b")
    AuthorProfileModel.objects.create(label="a1", author=a1)
    AuthorProfileModel.objects.create(label="n", author=None)
    AuthorProfileModel.objects.create(label="b", author=b)
    AuthorProfileModel.objects.create(label="a2", author=a2)
    resource = hasura_resource(
        ProfileNode,
        model=AuthorProfileModel,
        name="profiles",
        filterable=["id"],
        sortable=["author__name"],
        aggregatable=[],
        get_queryset=lambda info: AuthorProfileModel.objects.all(),
        write_backend=None,
        insert=False,
        update=False,
        delete=False,
    )
    schema = strawberry.Schema(query=resource.query, types=resource.types)
    result = schema.execute_sync(
        "{ profiles(order_by: [{author__name: " + member + "}]) { label } }"
    )
    assert result.errors is None, result.errors
    assert [row["label"] for row in result.data["profiles"]] == expected


@pytest.mark.parametrize("member", [value for value, _ in MEMBERS])
def test_public_id_column_uses_explicit_null_placement(nullable_rows, member):
    result = _row_schema().execute_sync(
        "{ null_rows(order_by: [{id: " + member + "}]) { id } }"
    )
    assert result.errors is None, result.errors
    expected = ["a1", "a2", "b", "n"]
    if member.startswith("desc"):
        expected.reverse()
    assert [row["id"] for row in result.data["null_rows"]] == expected


@pytest.mark.parametrize("member", [value for value, _ in MEMBERS])
def test_public_clauses_expose_selected_columns_and_placement(member):
    resource = hasura_resource(
        ReadBoundaryNode,
        model=ReadBoundaryModel,
        name="clause_rows",
        filterable=["id"],
        sortable=["id", "optional_title"],
        aggregatable=[],
        get_queryset=lambda info: ReadBoundaryModel.objects.all(),
        write_backend=None,
        insert=False,
        update=False,
        delete=False,
    )
    clauses = order_clauses(
        [resource.order_by_type(id=OrderBy(member))], id_column="code"
    )
    assert len(clauses) == 1
    assert isinstance(clauses[0].expression, F)
    assert clauses[0].expression.name == "code"
    assert clauses[0].descending == member.startswith("desc")
    assert clauses[0].nulls_first is (
        True
        if member in {"asc_nulls_first", "desc", "desc_nulls_first"}
        else None
    )
    assert clauses[0].nulls_last is (
        True
        if member in {"asc", "asc_nulls_last", "desc_nulls_last"}
        else None
    )


def test_wire_list_order_and_pk_tie_breaker(nullable_rows):
    resource = hasura_resource(
        ReadBoundaryNode,
        model=ReadBoundaryModel,
        name="sequence_rows",
        filterable=["id"],
        sortable=["id", "optional_title", "score"],
        aggregatable=[],
        get_queryset=lambda info: ReadBoundaryModel.objects.all(),
        write_backend=None,
        insert=False,
        update=False,
        delete=False,
    )
    schema = strawberry.Schema(query=resource.query, types=resource.types)
    result = schema.execute_sync(
        "{ sequence_rows(order_by: ["
        "{optional_title: asc_nulls_last}, {score: desc}]) { id } }"
    )
    assert result.errors is None, result.errors
    assert [row["id"] for row in result.data["sequence_rows"]] == [
        "a1",
        "a2",
        "b",
        "n",
    ]

    qs = ReadBoundaryModel.objects.all()
    selected = apply_ordering(
        qs,
        [resource.order_by_type(id=OrderBy.desc, score=OrderBy.asc)],
        id_column="code",
    )
    assert len(selected.query.order_by) == 2  # no duplicate PK
    assert [part.expression.name for part in selected.query.order_by] == [
        "code",
        "score",
    ]
    tied = apply_ordering(
        qs, [resource.order_by_type(optional_title=OrderBy.asc)]
    )
    assert [part.expression.name for part in tied.query.order_by] == [
        "optional_title",
        "pk",
    ]
    assert tied.query.order_by[-1].nulls_last is True
    assert apply_ordering(qs.order_by("-score"), []).query.order_by == (
        "-score",
    )
