"""Resource assembly must not alter another resource's schema contract."""

import strawberry
import strawberry_django
from strawberry import auto
from strawberry.tools import merge_types
from strawberry.types import get_object_definition

from strawberry_django_hasura import hasura_resource
from tests.demo_schema import NoteWriteBackend
from tests.models import NoteModel


def test_resource_preserves_shared_output_types():
    @strawberry.type
    class SharedChild:
        first_name: str

    @strawberry_django.type(NoteModel)
    class SharedNode:
        id: auto
        word_count: auto

        @strawberry.field
        def related_child(self) -> SharedChild:
            return SharedChild(first_name="Ada")

    @strawberry.type
    class Query:
        @strawberry.field
        def shared(self) -> SharedNode | None:
            return None

    before = str(strawberry.Schema(query=Query))
    hasura_resource(
        SharedNode,
        model=NoteModel,
        name="isolated_notes",
        filterable=["id"],
        sortable=["id"],
        aggregatable=["word_count"],
        get_queryset=lambda info: NoteModel.objects.all(),
        write_backend=NoteWriteBackend(),
        insert=False,
        update=False,
        delete=False,
    )
    after = str(strawberry.Schema(query=Query))
    assert before == after
    assert "wordCount" in after
    assert "relatedChild" in after
    assert "firstName" in after


def test_resources_share_node_without_sharing_aggregate_configuration(db):
    @strawberry_django.type(NoteModel)
    class SharedMeasureNode:
        id: auto

    def resource(name, measures):
        return hasura_resource(
            SharedMeasureNode,
            model=NoteModel,
            name=name,
            filterable=["id"],
            sortable=["id"],
            aggregatable=measures,
            get_queryset=lambda info: NoteModel.objects.all(),
            write_backend=NoteWriteBackend(),
            insert=False,
            update=False,
            delete=False,
        )

    measured = resource("measured_notes", ["word_count"])
    counted = resource("counted_notes", [])
    schema = strawberry.Schema(
        query=merge_types("Query", (measured.query, counted.query)),
        types=[*measured.types, *counted.types],
    )
    NoteModel.objects.create(title="One", word_count=7)
    result = schema.execute_sync("""
        {
          measured_notes_aggregate {
            aggregate { sum { word_count } }
          }
          counted_notes_aggregate { aggregate { count } }
        }
    """)
    assert result.errors is None, result.errors
    assert result.data["measured_notes_aggregate"]["aggregate"]["sum"] == {
        "word_count": "7"
    }
    assert result.data["counted_notes_aggregate"]["aggregate"]["count"] == 1
    invalid = schema.execute_sync("""
        { counted_notes_aggregate { aggregate { sum { word_count } } } }
    """)
    assert invalid.errors
    assert "Cannot query field 'sum'" in invalid.errors[0].message


def test_explicit_aggregate_name_preserves_legacy_prefix():
    @strawberry_django.type(NoteModel)
    class LegacyNode:
        id: auto

    resource = hasura_resource(
        LegacyNode,
        model=NoteModel,
        name="legacy_notes",
        filterable=["id"],
        sortable=["id"],
        aggregatable=["word_count"],
        aggregate_name="LegacyNote",
        get_queryset=lambda info: NoteModel.objects.all(),
        write_backend=NoteWriteBackend(),
    )
    assert get_object_definition(resource.aggregate_type).name == (
        "LegacyNoteAggregate"
    )


def test_same_resource_name_in_independent_schemas_keeps_recursive_inputs():
    @strawberry_django.type(NoteModel)
    class ReusedNode:
        id: auto

    def build(columns):
        return hasura_resource(
            ReusedNode,
            model=NoteModel,
            name="same_notes",
            filterable=columns,
            sortable=["id"],
            aggregatable=[],
            get_queryset=lambda info: NoteModel.objects.all(),
            write_backend=NoteWriteBackend(),
            insert=False,
            update=False,
            delete=False,
        )

    first = build(["id", "title"])
    second = build(["id", "status"])
    first_schema = strawberry.Schema(query=first.query, types=first.types)
    second_schema = strawberry.Schema(query=second.query, types=second.types)
    assert first_schema._schema.get_type("same_notes_bool_exp").fields[
        "_not"
    ].type is first_schema._schema.get_type("same_notes_bool_exp")
    assert (
        "title" in first_schema._schema.get_type("same_notes_bool_exp").fields
    )
    assert (
        "status"
        not in first_schema._schema.get_type("same_notes_bool_exp").fields
    )
    assert (
        "status"
        in second_schema._schema.get_type("same_notes_bool_exp").fields
    )
