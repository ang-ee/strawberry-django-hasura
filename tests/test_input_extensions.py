"""Converted input extensions reach writes without depending on a fork."""

from __future__ import annotations

import pytest
import strawberry

from strawberry_django_hasura import input_to_dict
from tests.models import BookModel
from tests.test_nested_insert import (
    BookWithChaptersWriteBackend,
    _book_with_chapters_resource,
)
from tests.test_write_boundaries import RecordingWriteBackend, _resource


def test_plain_input_preserves_python_names_order_and_unset_handling():
    @strawberry.input
    class BaseInput:
        title: str
        omitted: str | None = strawberry.UNSET

    @strawberry.input
    class PlainInput(BaseInput):
        word_count: int = strawberry.field(name="wordCount", default=0)
        nullable: str | None = None
        enabled: bool = False

    value = PlainInput(title="Plain")
    value.undeclared = "not an input field"

    assert list(input_to_dict(value).items()) == [
        ("title", "Plain"),
        ("word_count", 0),
        ("nullable", None),
        ("enabled", False),
    ]


@strawberry.input
class WriteExtension:
    write_only: str | None = strawberry.field(
        name="writeOnly", default=strawberry.UNSET
    )


@strawberry.input
class AuditExtension:
    audit_tag: str = strawberry.UNSET


@pytest.mark.parametrize("operation", ["insert", "update"])
@pytest.mark.parametrize(
    ("attributes", "expected"),
    [
        (
            {"write_only": "new value", "audit_tag": "reviewed"},
            {"write_only": "new value", "audit_tag": "reviewed"},
        ),
        ({"write_only": None}, {"write_only": None}),
        ({"write_only": strawberry.UNSET}, {}),
        ({}, {}),
    ],
)
def test_input_extensions_reach_create_and_update(
    operation, attributes, expected
):
    backend = RecordingWriteBackend()
    resource = _resource(backend)
    input_type = (
        resource.insert_input_type
        if operation == "insert"
        else resource.set_input_type
    )
    # Simulate the metadata and setattr calls made by an extension-capable
    # Strawberry converter, then exercise the generated resolver boundary.
    input_type.strawberry_input_extension_definitions = (
        WriteExtension.__strawberry_definition__,
        AuditExtension.__strawberry_definition__,
    )
    value = input_type(title="Example")
    for name, field_value in attributes.items():
        setattr(value, name, field_value)
    mutation = resource.mutation()

    if operation == "insert":
        mutation.insert_write_boundaries_one(info=None, object=value)
    else:
        mutation.update_write_boundaries_by_pk(
            info=None,
            pk_columns=resource.pk_columns_input_type(id="opaque-42"),
            _set=value,
        )

    assert backend.calls == [{"title": "Example", **expected}]
    assert list(backend.calls[0]) == ["title", *expected]


@strawberry.input
class AnnotationInput:
    label: str


@strawberry.input
class ChapterExtension:
    annotation: AnnotationInput | None = strawberry.UNSET


@pytest.mark.parametrize(
    "extension_value",
    [AnnotationInput(label="reviewed"), None, strawberry.UNSET],
)
def test_nested_insert_reduces_extension_fields(extension_value):
    class CapturingBackend(BookWithChaptersWriteBackend):
        def __init__(self):
            self.calls = []

        def create(self, info, data):
            self.calls.append(data)
            return BookModel(title=data["title"])

    backend = CapturingBackend()
    resource = _book_with_chapters_resource(write_backend=backend)
    child_type = resource.nested_input_types["chapters"]
    child_type.strawberry_input_extension_definitions = (
        ChapterExtension.__strawberry_definition__,
    )
    child = child_type(title="Parsing", position=1)
    child.annotation = extension_value
    value = resource.insert_input_type(
        title="Compiler",
        author="author-42",
        chapters=resource.nested_arr_input_types["chapters"](data=[child]),
    )

    resource.mutation().insert_books_one(info=None, object=value)

    expected_child = {"title": "Parsing", "position": 1}
    if extension_value is not strawberry.UNSET:
        expected_child["annotation"] = (
            None if extension_value is None else {"label": "reviewed"}
        )
    assert backend.calls == [
        {
            "title": "Compiler",
            "author": "author-42",
            "chapters": {"data": [expected_child]},
        }
    ]
    assert list(backend.calls[0]["chapters"]["data"][0]) == list(
        expected_child
    )
