"""Generated mutation inputs respect Django's write and scalar metadata."""

from __future__ import annotations

from typing import Any

import pytest
import strawberry
import strawberry_django
from django.core.files.uploadedfile import SimpleUploadedFile
from strawberry.scalars import JSON

from strawberry_django_hasura import hasura_resource
from tests.models import WriteBoundaryModel
from tests.test_async import _run_async


@strawberry_django.type(WriteBoundaryModel)
class WriteBoundaryNode:
    title: strawberry.auto
    revision: strawberry.auto


class RecordingWriteBackend:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def create(
        self, info: strawberry.Info, data: dict[str, Any]
    ) -> WriteBoundaryModel:
        self.calls.append(data)
        return WriteBoundaryModel(title=data.get("title", "response"))

    def update(
        self, info: strawberry.Info, pk: str, data: dict[str, Any]
    ) -> WriteBoundaryModel:
        return self.create(info, data)

    def delete(
        self, info: strawberry.Info, pk: str
    ) -> WriteBoundaryModel | None:
        raise AssertionError("delete is disabled")


def _resource(backend: RecordingWriteBackend, **overrides: Any):
    options: dict[str, Any] = {
        "model": WriteBoundaryModel,
        "name": "write_boundaries",
        "filterable": ["id", "owner"],
        "sortable": ["title"],
        "aggregatable": [],
        "get_queryset": lambda info: WriteBoundaryModel.objects.all(),
        "write_backend": backend,
        "delete": False,
    }
    options.update(overrides)
    return hasura_resource(WriteBoundaryNode, **options)


def _schema(backend: RecordingWriteBackend) -> strawberry.Schema:
    resource = _resource(backend)
    return strawberry.Schema(
        query=resource.query,
        mutation=resource.mutation,
        types=resource.types,
    )


def _input_block(sdl: str, name: str) -> str:
    return sdl.split(f"input {name} {{", 1)[1].split("}", 1)[0]


@pytest.mark.parametrize(
    "document",
    [
        """mutation {
          insert_write_boundaries_one(
            object: {title: "hello", protected_tags: ["1"]}
          ) { title }
        }""",
        """mutation {
          update_write_boundaries_by_pk(
            pk_columns: {id: "1"}, _set: {protected_tags: ["1"]}
          ) { title }
        }""",
    ],
)
def test_noneditable_m2m_is_rejected_before_backend(document):
    backend = RecordingWriteBackend()
    resource = _resource(backend)
    assert "protected_tags" not in resource.insertable_fields
    assert "protected_tags" not in resource.updatable_fields
    assert "allowed_tags" in resource.insertable_fields
    assert "allowed_tags" in resource.updatable_fields

    result = _schema(backend).execute_sync(document)

    assert result.errors
    assert "protected_tags" in result.errors[0].message
    assert "not defined" in result.errors[0].message
    assert backend.calls == []


@pytest.mark.parametrize("knob", ["writable", "insertable", "updatable"])
def test_explicit_allowlist_cannot_expose_noneditable_m2m(knob):
    with pytest.raises(TypeError, match="protected_tags.*not editable"):
        _resource(RecordingWriteBackend(), **{knob: ["protected_tags"]})


def test_one_to_one_uses_target_key_scalar_for_inputs_and_filters():
    backend = RecordingWriteBackend()
    schema = _schema(backend)
    sdl = schema.as_str()
    assert "owner: String" in _input_block(
        sdl, "write_boundaries_insert_input"
    )
    assert "owner: String" in _input_block(sdl, "write_boundaries_set_input")
    assert "owner: String_comparison_exp" in _input_block(
        sdl, "write_boundaries_bool_exp"
    )

    result = schema.execute_sync(
        """mutation {
          insert_write_boundaries_one(
            object: {title: "hello", owner: "person-42"}
          ) { title }
        }"""
    )

    assert result.errors is None
    assert backend.calls == [{"title": "hello", "owner": "person-42"}]


@pytest.mark.parametrize(
    "operation",
    [
        """mutation($upload: Upload!) {
          insert_write_boundaries_one(
            object: {title: "hello", document: $upload}
          ) { title }
        }""",
        """mutation($upload: Upload!) {
          update_write_boundaries_by_pk(
            pk_columns: {id: "1"}, _set: {document: $upload}
          ) { title }
        }""",
    ],
)
def test_file_upload_reaches_backend_unchanged(operation):
    backend = RecordingWriteBackend()
    schema = _schema(backend)
    for name in (
        "write_boundaries_insert_input",
        "write_boundaries_set_input",
    ):
        assert "document: Upload" in _input_block(schema.as_str(), name)
    upload = SimpleUploadedFile("note.txt", b"hello", "text/plain")

    result = schema.execute_sync(operation, variable_values={"upload": upload})

    assert result.errors is None
    assert len(backend.calls) == 1
    assert backend.calls[0]["document"] is upload


def test_omitted_database_default_is_applied_by_django(db):
    class PersistingWriteBackend(RecordingWriteBackend):
        def create(
            self, info: strawberry.Info, data: dict[str, Any]
        ) -> WriteBoundaryModel:
            self.calls.append(data)
            return WriteBoundaryModel.objects.create(**data)

    backend = PersistingWriteBackend()
    schema = _schema(backend)
    insert_input = _input_block(
        schema.as_str(), "write_boundaries_insert_input"
    )
    assert "revision: Int\n" in insert_input

    result = schema.execute_sync(
        """mutation {
          insert_write_boundaries_one(object: {title: "hello"}) {
            title revision
          }
        }"""
    )

    assert result.errors is None
    assert result.data == {
        "insert_write_boundaries_one": {"title": "hello", "revision": 7}
    }
    assert backend.calls == [{"title": "hello"}]
    assert WriteBoundaryModel.objects.get().revision == 7


class ArgumentWriteBackend(RecordingWriteBackend):
    def __init__(self) -> None:
        super().__init__()
        self.received: list[tuple[Any, ...]] = []

    def create(
        self, info: strawberry.Info, data: dict[str, Any], **extra: Any
    ) -> WriteBoundaryModel:
        self.received.append(("create", info.context, data, extra))
        return WriteBoundaryModel(title=data["title"])

    def update(
        self,
        info: strawberry.Info,
        pk: str,
        data: dict[str, Any],
        **extra: Any,
    ) -> WriteBoundaryModel:
        self.received.append(("update", info.context, pk, data, extra))
        return WriteBoundaryModel(title=data["title"])


_ARGUMENT_MUTATION = """mutation($key: String, $revision: Int) {
  insert_write_boundaries_one(
    object: {title: "new"}, client_creation_key: $key
  ) { title }
  update_write_boundaries_by_pk(
    pk_columns: {id: "opaque-42"}, _set: {title: "changed"},
    expected_revision: $revision
  ) { title }
}"""
_OMITTED_ARGUMENT_MUTATION = """mutation {
  insert_write_boundaries_one(object: {title: "new"}) { title }
  update_write_boundaries_by_pk(
    pk_columns: {id: "opaque-42"}, _set: {title: "changed"}
  ) { title }
}"""


@pytest.mark.parametrize("execute_async", [False, True])
@pytest.mark.parametrize(
    ("document", "arguments", "expected_insert", "expected_update"),
    [
        (
            _ARGUMENT_MUTATION,
            {"key": "attempt-42", "revision": 0},
            "attempt-42",
            0,
        ),
        (_ARGUMENT_MUTATION, {"key": None, "revision": None}, None, None),
        (_ARGUMENT_MUTATION, {}, None, None),
        (_OMITTED_ARGUMENT_MUTATION, {}, None, None),
    ],
)
def test_root_arguments_reach_backend_as_keywords(
    execute_async, document, arguments, expected_insert, expected_update
):
    backend = ArgumentWriteBackend()
    insert_arguments = {"client_creation_key": str}
    update_arguments = {"expected_revision": int}
    resource = _resource(
        backend,
        insert_arguments=insert_arguments,
        update_arguments=update_arguments,
    )
    # Declarations are copied at construction, just like other resource knobs.
    insert_arguments.clear()
    update_arguments.clear()
    schema = strawberry.Schema(
        query=resource.query,
        mutation=resource.mutation,
        types=resource.types,
    )
    context = object()
    execution_options = {
        "variable_values": arguments,
        "context_value": context,
    }
    result = (
        _run_async(schema.execute(document, **execution_options))
        if execute_async
        else schema.execute_sync(document, **execution_options)
    )

    assert result.errors is None, result.errors
    assert result.data == {
        "insert_write_boundaries_one": {"title": "new"},
        "update_write_boundaries_by_pk": {"title": "changed"},
    }
    assert backend.received == [
        (
            "create",
            context,
            {"title": "new"},
            {"client_creation_key": expected_insert},
        ),
        (
            "update",
            context,
            "opaque-42",
            {"title": "changed"},
            {"expected_revision": expected_update},
        ),
    ]
    assert resource.insert_argument_names == ("client_creation_key",)
    assert resource.update_argument_names == ("expected_revision",)


@pytest.mark.parametrize(
    "declarations",
    [
        {},
        {
            "insert_arguments": None,
            "update_arguments": None,
        },
        {"insert_arguments": {}, "update_arguments": {}},
    ],
)
def test_no_declaration_keeps_fixed_backend_signatures(declarations):
    backend = RecordingWriteBackend()
    resource = _resource(backend, **declarations)
    schema = strawberry.Schema(
        query=resource.query,
        mutation=resource.mutation,
        types=resource.types,
    )
    result = schema.execute_sync("""mutation {
      insert_write_boundaries_one(object: {title: "new"}) { title }
      update_write_boundaries_by_pk(
        pk_columns: {id: "opaque-42"}, _set: {title: "changed"}
      ) { title }
    }""")

    assert result.errors is None, result.errors
    assert backend.calls == [{"title": "new"}, {"title": "changed"}]
    assert resource.insert_argument_names == ()
    assert resource.update_argument_names == ()


def test_argument_input_types_use_strawberry_coercion_and_pass_through():
    @strawberry.input
    class WriteContext:
        reason: str

    backend = ArgumentWriteBackend()
    resource = _resource(
        backend,
        insert_arguments={
            "write_context": WriteContext,
            "labels": list[str],
            "retry": bool | None,
            "metadata": JSON,
        },
    )
    schema = strawberry.Schema(
        query=resource.query, mutation=resource.mutation
    )
    result = schema.execute_sync("""mutation {
      insert_write_boundaries_one(
        object: {title: "new"}, write_context: {reason: "test"},
        labels: ["first", "second"], retry: false, metadata: {attempt: 1}
      ) { title }
    }""")

    assert result.errors is None, result.errors
    assert backend.received[0][-1] == {
        "write_context": WriteContext(reason="test"),
        "labels": ["first", "second"],
        "retry": False,
        "metadata": {"attempt": 1},
    }
    assert resource.insert_argument_names == (
        "write_context",
        "labels",
        "retry",
        "metadata",
    )


@pytest.mark.parametrize("operation", ["insert", "update"])
@pytest.mark.parametrize(
    "name", ["self", "root", "info", "class", "bad-name", "9name", "", 42]
)
def test_invalid_or_resolver_reserved_argument_names_are_rejected(
    operation, name
):
    with pytest.raises(
        TypeError, match="invalid or reserved Python parameter name"
    ):
        _resource(
            ArgumentWriteBackend(), **{f"{operation}_arguments": {name: str}}
        )


def test_declared_argument_type_is_validated_before_backend():
    backend = ArgumentWriteBackend()
    resource = _resource(backend, update_arguments={"expected_revision": int})
    schema = strawberry.Schema(
        query=resource.query, mutation=resource.mutation
    )
    result = schema.execute_sync("""mutation {
      update_write_boundaries_by_pk(
        pk_columns: {id: "opaque-42"}, _set: {title: "changed"},
        expected_revision: "invalid"
      ) { title }
    }""")

    assert result.errors
    assert backend.received == []
