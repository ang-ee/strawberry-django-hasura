"""Resource-local configuration composes native aggregate and filter owners."""

from __future__ import annotations

import pytest
import strawberry
import strawberry_django
from strawberry import auto

from strawberry_django_hasura import HasuraResource, hasura_resource
from tests.demo_schema import Note, NoteWriteBackend
from tests.models import AuthorModel, BookModel, ChapterModel, NoteModel
from tests.test_write_boundaries import RecordingWriteBackend, _resource


@pytest.mark.parametrize("operation", ["insert", "update"])
@pytest.mark.parametrize("name", ["object", "pk_columns", "_set"])
def test_reserved_mutation_argument_names_fail_at_construction(
    operation, name
):
    with pytest.raises(TypeError, match=rf"{operation}_arguments.*reserved"):
        _resource(
            RecordingWriteBackend(),
            **{f"{operation}_arguments": {name: str}},
        )


@pytest.mark.parametrize("operation", ["insert", "update"])
def test_mutation_arguments_require_enabled_operation(operation):
    with pytest.raises(TypeError, match=rf"requires {operation}=True"):
        _resource(
            RecordingWriteBackend(),
            **{operation: False, f"{operation}_arguments": {"reason": str}},
        )


@pytest.mark.parametrize(
    ("operation", "method"), [("insert", "create"), ("update", "update")]
)
@pytest.mark.parametrize("positional_only", [False, True])
def test_mutation_arguments_require_backend_keyword_support(
    operation, method, positional_only
):
    backend = RecordingWriteBackend()
    if positional_only:

        def callback(reason, /, *args):
            raise AssertionError("construction must not call the backend")

        setattr(backend, method, callback)

    with pytest.raises(TypeError) as error:
        _resource(backend, **{f"{operation}_arguments": {"reason": str}})

    assert str(error.value) == (
        f"write_backend.{method} does not accept "
        f"{operation}_arguments ['reason']"
    )


@pytest.mark.parametrize(
    ("operation", "method"), [("insert", "create"), ("update", "update")]
)
@pytest.mark.parametrize("keyword_only", [False, True])
def test_mutation_arguments_accept_explicit_backend_parameters(
    operation, method, keyword_only
):
    def positional(info, pk_or_data, data=None, reason=None):
        raise AssertionError("construction must not call the backend")

    def keyword(*args, reason=None):
        raise AssertionError("construction must not call the backend")

    backend = RecordingWriteBackend()
    setattr(backend, method, keyword if keyword_only else positional)
    resource = _resource(
        backend, **{f"{operation}_arguments": {"reason": str}}
    )
    assert getattr(resource, f"{operation}_argument_names") == ("reason",)
    with pytest.raises(TypeError, match=r"arguments \['unsupported'\]"):
        _resource(
            backend,
            **{f"{operation}_arguments": {"reason": str, "unsupported": int}},
        )


@pytest.mark.parametrize(
    ("operation", "method"), [("insert", "create"), ("update", "update")]
)
@pytest.mark.parametrize("error_type", [TypeError, ValueError])
def test_mutation_arguments_allow_uninspectable_backend(
    operation, method, error_type
):
    class OpaqueCallable:
        @property
        def __signature__(self):
            raise error_type("signature unavailable")

        def __call__(self, *args, **kwargs):
            raise AssertionError("construction must not call the backend")

    backend = RecordingWriteBackend()
    setattr(backend, method, OpaqueCallable())
    resource = _resource(
        backend, **{f"{operation}_arguments": {"reason": str}}
    )
    assert getattr(resource, f"{operation}_argument_names") == ("reason",)


@pytest.mark.parametrize("operation", ["insert", "update"])
@pytest.mark.parametrize("argument_type", ["int", None])
def test_mutation_argument_types_must_not_be_strings_or_none(
    operation, argument_type
):
    with pytest.raises(
        TypeError,
        match="must be an input type, not a string annotation or None",
    ):
        _resource(
            RecordingWriteBackend(),
            **{f"{operation}_arguments": {"reason": argument_type}},
        )


def test_argument_metadata_defaults_empty():
    resource = HasuraResource(
        type("Query", (), {}), type("Mutation", (), {}), []
    )
    assert resource.insert_argument_names == ()
    assert resource.update_argument_names == ()


def test_empty_mutation_argument_mappings_allow_disabled_operations():
    resource = note_resource(
        aggregatable=[], insert_arguments={}, update_arguments={}
    )
    assert resource.insert_argument_names == ()
    assert resource.update_argument_names == ()


def note_resource(name="json_notes", **kwargs):
    return hasura_resource(
        Note,
        model=NoteModel,
        name=name,
        filterable=["title", "status"],
        sortable=["title"],
        get_queryset=lambda info: NoteModel.objects.all(),
        write_backend=NoteWriteBackend(),
        insert=False,
        update=False,
        delete=False,
        **kwargs,
    )


@pytest.mark.django_db
def test_json_measures_groups_having_counts_and_configuration_snapshot():
    NoteModel.objects.bulk_create(
        [
            NoteModel(
                title="first", metadata={"region": "north", "amount": 5}
            ),
            NoteModel(
                title="second", metadata={"region": "north", "amount": 7}
            ),
            NoteModel(
                title="third", metadata={"region": "south", "amount": 3}
            ),
        ]
    )
    paths = {"metadata.region": "str", "metadata.amount": "int"}
    resource = note_resource(
        aggregatable=["metadata.amount"],
        groupable=["metadata.region"],
        json_paths=paths,
    )
    paths.clear()
    schema = strawberry.Schema(query=resource.query)
    result = schema.execute_sync("""
        query {
          total: json_notes_aggregate {
            aggregate { count sum { metadata__amount } }
          }
          groups: json_notes_groups(
            group_by: [{field: METADATA__REGION}]
            having: {sum_metadata__amount_gt: 4}
          ) {
            key { metadata__region }
            aggregate { count sum { metadata__amount } }
          }
          count: json_notes_groups_count(
            group_by: [{field: METADATA__REGION}]
            having: {sum_metadata__amount_gt: 4}
          )
        }
    """)
    assert result.errors is None, result.errors
    assert result.data["total"]["aggregate"]["count"] == 3
    assert (
        int(result.data["total"]["aggregate"]["sum"]["metadata__amount"]) == 15
    )
    assert result.data["count"] == 1
    group = result.data["groups"][0]
    assert group["key"] == {"metadata__region": "north"}
    assert int(group["aggregate"]["sum"]["metadata__amount"]) == 12


@pytest.mark.django_db
def test_backend_lookup_is_resource_local_and_recursive():
    NoteModel.objects.create(title="Alpha", status="published")
    NoteModel.objects.create(title="Bravo", status="draft")
    extension = {"iregex": ("__iregex", False)}
    extended = note_resource(
        "extended_notes",
        aggregatable=[],
        groupable=["status"],
        filter_lookups=extension,
    )
    portable = note_resource("portable_notes", aggregatable=[])
    extension.clear()
    schema = strawberry.Schema(query=extended.query)
    result = schema.execute_sync("""
        query {
          rows: extended_notes(where: {_and: [{_or: [
            {title: {_iregex: "^al"}},
            {_not: {title: {_iregex: ".*"}}}
          ]}]}) { title }
          aggregate: extended_notes_aggregate(
            where: {title: {_iregex: "^al"}}
          ) { aggregate { count } }
          groups: extended_notes_groups(
            group_by: [{field: STATUS}], where: {title: {_iregex: "^al"}}
          ) { aggregate { count } }
          count: extended_notes_groups_count(
            group_by: [{field: STATUS}], where: {title: {_iregex: "^al"}}
          )
        }
    """)
    assert result.errors is None, result.errors
    assert result.data == {
        "rows": [{"title": "Alpha"}],
        "aggregate": {"aggregate": {"count": 1}},
        "groups": [{"aggregate": {"count": 1}}],
        "count": 1,
    }
    rejected = strawberry.Schema(query=portable.query).execute_sync(
        '{portable_notes(where: {title: {_iregex: "^al"}}) {title}}'
    )
    assert rejected.errors
    assert "not mapped" in str(rejected.errors[0])


@pytest.mark.parametrize(
    "lookups",
    [
        {"eq": ("__gte", False)},
        {"is_null": ("__isnull", False)},
        {"iregex": ("iregex", False)},
        {"iregex": ("__iregex", "false")},
    ],
)
def test_invalid_lookup_configuration_fails_at_construction(lookups):
    with pytest.raises(ValueError):
        note_resource(aggregatable=[], filter_lookups=lookups)


def test_ambiguous_json_alias_fails_at_construction():
    with pytest.raises(ValueError, match="share alias"):
        note_resource(
            aggregatable=["metadata__amount"],
            json_paths={"metadata.amount": "int"},
        )


@pytest.mark.django_db
def test_nested_relation_key_codec_changes_only_output_identity():
    author = AuthorModel.objects.create(name="Alice")
    book = BookModel.objects.create(title="Book", author=author)
    ChapterModel.objects.create(title="One", book=book)
    ChapterModel.objects.create(title="Two", book=book)

    @strawberry_django.type(ChapterModel)
    class Chapter:
        title: auto

    encoded = []

    def encode(value):
        encoded.append(value)
        return f"author-{value}"

    codecs = {"book__author": encode}
    resource = hasura_resource(
        Chapter,
        model=ChapterModel,
        name="encoded_chapters",
        filterable=["book__author"],
        sortable=["title"],
        aggregatable=["position"],
        groupable=["book__author"],
        group_key_encoders=codecs,
        field_id_decode={"book__author": lambda value: int(value[7:])},
        get_queryset=lambda info: ChapterModel.objects.all(),
        write_backend=NoteWriteBackend(),
        insert=False,
        update=False,
        delete=False,
    )
    codecs.clear()
    schema = strawberry.Schema(query=resource.query)
    result = schema.execute_sync("""
        {
          groups: encoded_chapters_groups(
            group_by: [{field: BOOK__AUTHOR}]
          ) { key {book__author_id} aggregate {count sum {position}} }
          count: encoded_chapters_groups_count(
            group_by: [{field: BOOK__AUTHOR}]
          )
        }
    """)
    assert result.errors is None, result.errors
    assert result.data["count"] == 1
    key = result.data["groups"][0]["key"]["book__author_id"]
    assert key == f"author-{author.pk}"
    assert result.data["groups"][0]["aggregate"]["count"] == 2
    assert encoded == [author.pk]
    filtered = schema.execute_sync(
        """query($id: String!) {
          encoded_chapters(where: {book__author: {_eq: $id}}) {title}
        }""",
        variable_values={"id": key},
    )
    assert filtered.errors is None, filtered.errors
    assert len(filtered.data["encoded_chapters"]) == 2


def test_group_key_codec_requires_declared_axis():
    with pytest.raises(ValueError, match="declared groupable"):
        note_resource(aggregatable=[], group_key_encoders={"title": str})


@pytest.mark.django_db
def test_nullable_one_to_one_key_keeps_native_alias_and_null():
    from tests.models import AuthorProfileModel

    author = AuthorModel.objects.create(name="Alice")
    AuthorProfileModel.objects.create(author=author, label="profile")
    AuthorProfileModel.objects.create(author=None, label="unassigned")

    @strawberry_django.type(AuthorProfileModel)
    class Profile:
        label: auto

    calls = []

    def encode(value):
        calls.append(value)
        return f"author-{value}"

    resource = hasura_resource(
        Profile,
        model=AuthorProfileModel,
        name="profiles",
        filterable=["author"],
        sortable=["label"],
        aggregatable=["id"],
        groupable=["author"],
        group_key_encoders={"author": encode},
        field_id_decode={"author": lambda value: int(value[7:])},
        get_queryset=lambda info: AuthorProfileModel.objects.all(),
        write_backend=NoteWriteBackend(),
        insert=False,
        update=False,
        delete=False,
    )
    result = strawberry.Schema(query=resource.query).execute_sync("""
        {
          profiles_groups(group_by: [{field: AUTHOR}]) {
            key {author} aggregate {count}
          }
        }
    """)
    assert result.errors is None, result.errors
    groups = result.data["profiles_groups"]
    assert {group["key"]["author"] for group in groups} == {
        None,
        f"author-{author.pk}",
    }
    assert [group["aggregate"]["count"] for group in groups] == [1, 1]
    assert calls == [author.pk]
