"""Assert the emitted SDL carries the stock ``@refinedev/hasura`` shape.

The contract is ``CONTRACT.md``; these markers are the load-bearing pieces of
it the provider references. Converted from the spike's rendered Hasura SDL.
"""

from __future__ import annotations

import datetime
import re
from typing import Any

import pytest
import strawberry
import strawberry_django
from strawberry import auto

from strawberry_django_hasura import (
    FilterablePathError,
    InMemoryRowSource,
    hasura_resource,
    hasura_run_query_resource,
)
from tests.models import AuthorModel, BookModel, ChapterModel


def test_temporal_comparisons_and_resource_aggregate_prefix():
    @strawberry.type
    class TemporalRow:
        id: str
        date: datetime.date
        time: datetime.time

    resource = hasura_run_query_resource(
        TemporalRow,
        name="temporal_rows",
        filterable=["id", "date", "time"],
        sortable=["id"],
        source=InMemoryRowSource(lambda info: []),
    )
    sdl = str(strawberry.Schema(query=resource.query, types=resource.types))
    for marker in (
        "date: Date_comparison_exp",
        "time: Time_comparison_exp",
        "input Date_comparison_exp {",
        "input Time_comparison_exp {",
        "_eq: Date",
        "_eq: Time",
        "aggregate: temporal_rowsAggregate!",
    ):
        assert marker in sdl


# CRUD surface markers — list (where/order_by/limit/offset), by-pk, mutations,
# the snake_case wire convention, and the String-typed pk surface
# (CONTRACT.md "Queries" / "Mutations" / "Filter" / "order_by").
CRUD_MARKERS = [
    "type Note {",
    "notes(where: notes_bool_exp",
    "order_by: [notes_order_by!]",
    "limit: Int",
    "offset: Int",
    "): [Note!]!",
    "notes_by_pk(id: String!): Note",
    "insert_notes_one(object: notes_insert_input!): Note!",
    "update_notes_by_pk(pk_columns: notes_pk_columns_input!,"
    " _set: notes_set_input!): Note!",
    "delete_notes_by_pk(id: String!): Note",
    "input notes_bool_exp {",
    "input notes_order_by {",
    "input notes_insert_input {",
    "input notes_set_input {",
    "input notes_pk_columns_input {",
    "id: String!",  # the pk surface is String, not ID (refine idType)
    "_eq",
    "_neq",
    "_ilike",
    "_in",
    "_is_null",
    "input JSON_comparison_exp {",
    "_contains",
    "metadata: JSON_comparison_exp",
    "_and: [notes_bool_exp!]",
    "_or: [notes_bool_exp!]",
    "_not: notes_bool_exp",
    "enum order_by {",
    "word_count",  # snake_case verbatim on the wire (Hasura convention)
]

# The free-aggregate surface markers — the native ``<Model>Aggregate`` type IS
# Hasura's ``aggregate {…}`` (CONTRACT.md "Aggregate"). NO reshape layer.
AGGREGATE_MARKERS = [
    "notes_aggregate(where: notes_bool_exp",
    "): notes_aggregate!",
    "type notes_aggregate {",
    "aggregate: NoteAggregate!",
    "nodes: [Note!]!",
    "type NoteAggregate {",
    "count: Int!",
    "sum: NoteSumFields",
    "avg: NoteAvgFields",
    "min: NoteMinFields",
    "max: NoteMaxFields",
    "type NoteSumFields {",
    "word_count: BigInt",  # SUM over an IntegerField widens to BigInt
    "price: Decimal",  # SUM over a DecimalField stays exact Decimal, not Float
]

# Decimal wire markers (F1) — a ``DecimalField`` column filters through the
# exact ``Decimal_comparison_exp`` (strawberry ``Decimal`` operands, exact
# strings on the wire), NOT ``Float_comparison_exp``; the node field and the
# aggregate measures stay ``Decimal`` too (never Float).
DECIMAL_MARKERS = [
    "input Decimal_comparison_exp {",
    "price: Decimal_comparison_exp",  # on notes_bool_exp
    "price: Decimal!",  # the Note node field (exact scalar, not Float)
    "scalar Decimal",
]

# Grouping — NDC-preview surface (CONTRACT.md "Grouping — NDC preview"). The
# `<res>_group` pairs the typed `<Model>GroupKey` with the FREE
# `<Model>Aggregate` (no reshape); emitted because the demo uses `groupable`.
GROUPING_MARKERS = [
    "notes_groups(group_by: [NoteGroupBySpec!]!",
    "notes_groups_count(group_by: [NoteGroupBySpec!]!,"
    " where: notes_bool_exp = null, having: NoteHaving = null): Int!",
    "): [notes_group!]!",
    "type notes_group {",
    "key: NoteGroupKey!",
    "aggregate: NoteAggregate!",  # the SAME free aggregate — no reshape
    "input NoteGroupBySpec {",
    "input NoteHaving {",
    "count_gt: Int",  # snake_case having operator (not camelCased)
    "type NoteGroupKey {",
    "updated_at_month_range: BucketRange",  # TIME bucket-range sibling
    "enum NoteGroupableField {",
]


@pytest.mark.parametrize("marker", CRUD_MARKERS)
def test_crud_marker_present(schema, marker):
    assert marker in schema.as_str()


@pytest.mark.parametrize("marker", AGGREGATE_MARKERS)
def test_aggregate_marker_present(schema, marker):
    assert marker in schema.as_str()


@pytest.mark.parametrize("marker", GROUPING_MARKERS)
def test_grouping_marker_present(schema, marker):
    assert marker in schema.as_str()


@pytest.mark.parametrize("marker", DECIMAL_MARKERS)
def test_decimal_marker_present(schema, marker):
    assert marker in schema.as_str()


def test_decimal_column_does_not_degrade_to_float(schema):
    """A ``DecimalField`` filters as exact ``Decimal`` and its SUM/AVG/MIN/MAX
    measures stay ``Decimal`` — the F1 wire fix. (STDDEV/VARIANCE over a
    decimal are inherently Float — a statistical result, not a degradation — so
    only the exact-valued ops are asserted.)"""
    sdl = schema.as_str()
    # The comparison input is exact Decimal, never Float.
    assert "price: Float_comparison_exp" not in sdl
    assert "price: Decimal_comparison_exp" in sdl
    # The exact-valued aggregate measures keep the column's Decimal scalar.
    for block in ("Sum", "Avg", "Min", "Max"):
        match = re.search(rf"type Note{block}Fields \{{[^}}]*\}}", sdl)
        assert match is not None, f"Note{block}Fields missing"
        assert "price: Decimal" in match.group(0)
        assert "price: Float" not in match.group(0)


def test_grouped_aggregate_is_the_free_type_no_reshape(schema):
    """`<res>_group.aggregate` is the SAME free `NoteAggregate` — the grouped
    surface composes the upstream typed key + free aggregate; it does not
    reshape into a grouped envelope, and the old stringly-typed `KeyValue`
    shape is gone."""
    sdl = schema.as_str()
    assert "type notes_group {" in sdl
    assert "aggregate: NoteAggregate!" in sdl
    assert "AggregateResponse" not in sdl
    assert "KeyValue" not in sdl


def test_pk_args_are_string_not_id(schema):
    """Every pk-arg surface is GraphQL ``String`` so refine's
    ``idType: "String"`` (``$id: String!``) binds; the output ``Note.id``
    stays ``ID`` (serializes a string fine on output)."""
    sdl = schema.as_str()
    assert "notes_by_pk(id: String!)" in sdl
    assert "delete_notes_by_pk(id: String!)" in sdl
    assert "id: String!" in sdl  # notes_pk_columns_input.id
    assert "  id: ID!" in sdl  # the Note output field is still ID


def test_aggregate_is_the_native_type_no_reshape(schema):
    """The ``aggregate`` field is the library's own ``<Model>Aggregate`` —
    proof there is no nestjs-style ``<Model>AggregateResponse`` reshape."""
    sdl = schema.as_str()
    assert "aggregate: NoteAggregate!" in sdl
    assert "AggregateResponse" not in sdl


@strawberry_django.type(ChapterModel)
class FilterableChapter:
    """A relation-chain node used only by the nested-filter contract tests."""

    title: auto


class _NoWrites:
    def create(self, info: Any, data: dict[str, Any]) -> Any: ...

    def update(self, info: Any, pk: str, data: dict[str, Any]) -> Any: ...

    def delete(self, info: Any, pk: str) -> Any: ...


def _chapter_resource(
    *,
    filterable: list[str] | None = None,
    groupable: list[str] | None = None,
):
    return hasura_resource(
        FilterableChapter,
        model=ChapterModel,
        name="chapters",
        filterable=(
            ["title", "book", "book__author"]
            if filterable is None
            else filterable
        ),
        sortable=["title"],
        aggregatable=[],
        groupable=groupable,
        get_queryset=lambda info: ChapterModel.objects.all(),
        write_backend=_NoWrites(),
        insert=False,
        update=False,
        delete=False,
    )


def _chapter_schema(*, groupable: list[str] | None = None):
    resource = _chapter_resource(groupable=groupable)
    return strawberry.Schema(query=resource.query, types=resource.types)


def test_nested_to_one_filter_path_extends_direct_relation_sdl_shape():
    """Direct and nested relations are both flat terminal-key comparisons.

    The complete ``__`` path is the field name in the existing resource
    bool-exp. No parallel relation bool-exp type is generated, so a caller may
    expose both ``book`` and ``book__author`` without a type collision or a
    behavior change to the direct field.
    """
    sdl = _chapter_schema().as_str()
    match = re.search(r"input chapters_bool_exp \{[^}]*\}", sdl)

    assert match is not None
    block = match.group(0)
    assert "book: ID_comparison_exp" in block
    assert "book__author: ID_comparison_exp" in block
    assert "chapters_book_bool_exp" not in sdl


def test_nested_scalar_terminal_uses_the_terminal_fields_scalar():
    """A ``__`` path may end at a scalar; the comparison follows that scalar.

    ``book__title`` traverses the to-one ``book`` and compares the CharField
    terminal, so the bool-exp entry is a String comparison, exactly like a
    direct scalar column.
    """
    resource = _chapter_resource(filterable=["title", "book__title"])
    sdl = strawberry.Schema(
        query=resource.query, types=resource.types
    ).as_str()
    match = re.search(r"input chapters_bool_exp \{[^}]*\}", sdl)

    assert match is not None
    assert "book__title: String_comparison_exp" in match.group(0)


@pytest.mark.django_db
def test_nested_terminal_fk_filter_accepts_the_group_bucket_value():
    """A terminal-FK group key can drill into the same relation path."""
    ada = AuthorModel.objects.create(name="Ada")
    grace = AuthorModel.objects.create(name="Grace")
    compiler = BookModel.objects.create(title="Compiler", author=ada)
    systems = BookModel.objects.create(title="Systems", author=grace)
    ChapterModel.objects.create(book=compiler, title="Parsing", position=1)
    ChapterModel.objects.create(book=compiler, title="Types", position=2)
    ChapterModel.objects.create(book=systems, title="Processes", position=1)
    chapter_schema = _chapter_schema(groupable=["book__author"])

    grouped = chapter_schema.execute_sync(
        """
        query {
          chapters_groups(group_by: [{field: BOOK__AUTHOR}]) {
            key { book__author_id }
            aggregate { count }
          }
        }
        """
    )
    assert grouped.errors is None, grouped.errors
    ada_bucket = next(
        group
        for group in grouped.data["chapters_groups"]
        if group["key"]["book__author_id"] == str(ada.pk)
    )
    grouped_value = ada_bucket["key"]["book__author_id"]
    assert isinstance(grouped_value, str)
    assert ada_bucket["aggregate"]["count"] == 2

    filtered = chapter_schema.execute_sync(
        """
        query($where: chapters_bool_exp) {
          chapters(where: $where, order_by: [{title: asc}]) { title }
        }
        """,
        variable_values={"where": {"book__author": {"_eq": grouped_value}}},
    )
    assert filtered.errors is None, filtered.errors
    assert filtered.data["chapters"] == [
        {"title": "Parsing"},
        {"title": "Types"},
    ]


@pytest.mark.django_db
def test_direct_relation_filter_keeps_its_existing_scalar_shape():
    """Adding path support does not reinterpret a direct FK as a bool-exp."""
    author = AuthorModel.objects.create(name="Ada")
    first = BookModel.objects.create(title="First", author=author)
    second = BookModel.objects.create(title="Second", author=author)
    ChapterModel.objects.create(book=first, title="Keep", position=1)
    ChapterModel.objects.create(book=second, title="Drop", position=1)

    result = _chapter_schema().execute_sync(
        """
        query($where: chapters_bool_exp) {
          chapters(where: $where) { title }
        }
        """,
        variable_values={"where": {"book": {"_eq": str(first.pk)}}},
    )

    assert result.errors is None, result.errors
    assert result.data["chapters"] == [{"title": "Keep"}]


def test_filterable_path_rejects_a_to_many_segment_at_build_time():
    with pytest.raises(
        FilterablePathError,
        match=(
            r"filterable path 'book__tags__name'.*"
            r"to-many relation 'tags'.*only to-one"
        ),
    ):
        _chapter_resource(filterable=["book__tags__name"])


def test_filterable_path_rejects_a_to_many_terminal_at_build_time():
    with pytest.raises(
        FilterablePathError,
        match=(
            r"filterable path 'book__tags'.*"
            r"to-many relation 'tags'.*only to-one"
        ),
    ):
        _chapter_resource(filterable=["book__tags"])


def test_filterable_path_rejects_an_unknown_terminal_at_build_time():
    with pytest.raises(
        FilterablePathError,
        match=(
            r"filterable path 'book__publisher'.*terminates at "
            r"'publisher'.*neither a relation nor a scalar field"
        ),
    ):
        _chapter_resource(filterable=["book__publisher"])


def test_declared_sort_alias_is_a_wire_enum_field_only():
    resource = hasura_resource(
        FilterableChapter,
        model=ChapterModel,
        name="sort_chapters",
        filterable=["title"],
        sortable=["display_title"],
        sortable_aliases={"display_title": "_title"},
        aggregatable=[],
        get_queryset=lambda info: ChapterModel.objects.all(),
        write_backend=_NoWrites(),
        insert=False,
        update=False,
        delete=False,
    )
    sdl = strawberry.Schema(
        query=resource.query, types=resource.types
    ).as_str()
    assert (
        "input sort_chapters_order_by {\n  display_title: order_by\n}" in sdl
    )
    assert "\n  _title:" not in sdl
    assert "order_by: [sort_chapters_order_by!]" in sdl
