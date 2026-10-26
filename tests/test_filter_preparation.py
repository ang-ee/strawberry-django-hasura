"""Lazy queryset expressions compose the translated filter boundary."""

import pytest
import strawberry
from django.db.models import F, Q

from strawberry_django_hasura import hasura_resource
from strawberry_django_hasura.filtering import filter_queryset
from tests.demo_schema import Note, NoteWriteBackend
from tests.models import NoteModel


@pytest.mark.django_db
def test_preparation_installs_alias_before_filter_without_losing_scope():
    NoteModel.objects.create(title="visible", word_count=12)
    NoteModel.objects.create(title="hidden", word_count=12)
    scoped = NoteModel.objects.filter(title="visible")
    calls = []

    def prepare(queryset, predicate):
        calls.append(predicate)
        return queryset.alias(doubled=F("word_count") * 2)

    predicate = Q(doubled__gt=20)
    result = filter_queryset(scoped, predicate, prepare_queryset=prepare)
    assert result.count() == 1
    assert list(result.values_list("title", flat=True)) == ["visible"]
    assert calls == [predicate]


@pytest.mark.django_db
@pytest.mark.parametrize(
    "where",
    [
        None,
        {
            "_and": [
                {
                    "_or": [
                        {"title": {"_eq": "visible"}},
                        {"_not": {"title": {"_eq": "visible"}}},
                    ]
                }
            ]
        },
    ],
)
def test_resource_prepares_each_filtered_surface_once(where):
    NoteModel.objects.create(title="visible", status="published")
    NoteModel.objects.create(title="hidden", status="draft")
    predicates = []

    def prepare(queryset, predicate):
        predicates.append(predicate)
        assert list(queryset.values_list("title", flat=True)) == ["visible"]
        return queryset

    resource = hasura_resource(
        Note,
        model=NoteModel,
        name="prepared_notes",
        filterable=["title"],
        sortable=["title"],
        get_queryset=lambda info: NoteModel.objects.filter(title="visible"),
        write_backend=NoteWriteBackend(),
        insert=False,
        update=False,
        delete=False,
        aggregatable=[],
        groupable=["status"],
        prepare_filter_queryset=prepare,
    )
    result = strawberry.Schema(query=resource.query).execute_sync(
        """query($where: prepared_notes_bool_exp) {
          rows: prepared_notes(where: $where) { title }
          total: prepared_notes_aggregate(where: $where) {
            aggregate { count } nodes { title }
          }
          groups: prepared_notes_groups(
            where: $where, group_by: [{field: STATUS}]
          ) { aggregate { count } }
          count: prepared_notes_groups_count(
            where: $where, group_by: [{field: STATUS}]
          )
        }""",
        variable_values={"where": where},
    )
    assert result.errors is None, result.errors
    assert result.data == {
        "rows": [{"title": "visible"}],
        "total": {"aggregate": {"count": 1}, "nodes": [{"title": "visible"}]},
        "groups": [{"aggregate": {"count": 1}}],
        "count": 1,
    }
    assert len(predicates) == 5
    assert all(isinstance(predicate, Q) for predicate in predicates)
    assert all(
        bool(predicate) == (where is not None) for predicate in predicates
    )


@pytest.mark.django_db
def test_preparation_keeps_to_many_membership_counts_distinct():
    from tests.models import AuthorModel, BookModel, TagModel

    author = AuthorModel.objects.create(name="Author")
    visible = BookModel.objects.create(title="visible", author=author)
    hidden = BookModel.objects.create(title="hidden", author=author)
    tags = [TagModel.objects.create(name=name) for name in ["one", "two"]]
    visible.tags.add(*tags)
    hidden.tags.add(*tags)
    calls = []

    def prepare(queryset, predicate):
        calls.append(predicate)
        return queryset.alias(lazy_title=F("title"))

    result = filter_queryset(
        BookModel.objects.filter(pk=visible.pk),
        Q(tags__name__in=["one", "two"]) & Q(lazy_title="visible"),
        prepare_queryset=prepare,
    )
    assert result.count() == 1
    assert list(result.values_list("pk", flat=True)) == [visible.pk]
    assert len(calls) == 1
