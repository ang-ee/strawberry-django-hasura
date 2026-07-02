"""The built resource exposes the roles it already knows."""

from __future__ import annotations

from strawberry.types import get_object_definition

from tests.demo_schema import Note, resource


def _graphql_name(strawberry_type: type) -> str:
    definition = get_object_definition(strawberry_type)
    assert definition is not None
    return definition.name


def test_model_resource_exposes_role_named_types_and_roots():
    assert resource.node_type is Note
    assert _graphql_name(resource.filter_type) == "notes_bool_exp"
    assert _graphql_name(resource.order_by_type) == "notes_order_by"
    assert _graphql_name(resource.insert_input_type) == "notes_insert_input"
    assert _graphql_name(resource.set_input_type) == "notes_set_input"
    assert _graphql_name(resource.pk_columns_input_type) == (
        "notes_pk_columns_input"
    )
    assert _graphql_name(resource.aggregate_type) == "NoteAggregate"
    assert _graphql_name(resource.group_key_type) == "NoteGroupKey"

    assert resource.list_root == "notes"
    assert resource.aggregate_root == "notes_aggregate"
    assert resource.detail_root == "notes_by_pk"
    assert resource.groups_root == "notes_groups"
    assert resource.insert_one_root == "insert_notes_one"
    assert resource.update_by_pk_root == "update_notes_by_pk"
    assert resource.delete_by_pk_root == "delete_notes_by_pk"


def test_model_resource_exposes_builder_decided_write_facts():
    assert resource.enabled_operations == ("insert", "update", "delete")
    assert resource.insertable_fields == (
        "title",
        "word_count",
        "is_starred",
        "status",
        "metadata",
    )
    assert resource.updatable_fields == (
        "title",
        "word_count",
        "is_starred",
        "status",
        "metadata",
    )
