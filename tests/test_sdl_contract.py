"""Assert the emitted SDL carries the stock ``@refinedev/hasura`` shape.

The contract is ``CONTRACT.md``; these markers are the load-bearing pieces of
it the provider references. Converted from the spike's rendered Hasura SDL.
"""

from __future__ import annotations

import pytest

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
]


@pytest.mark.parametrize("marker", CRUD_MARKERS)
def test_crud_marker_present(schema, marker):
    assert marker in schema.as_str()


@pytest.mark.parametrize("marker", AGGREGATE_MARKERS)
def test_aggregate_marker_present(schema, marker):
    assert marker in schema.as_str()


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
