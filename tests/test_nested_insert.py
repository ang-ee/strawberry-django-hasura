"""Hasura-native nested object insert for a declared to-many relation.

A ``NestedInsert`` declaration exposes ``insert_<res>_one(object: {...,
<relation>: {data: [<child>...]}})`` — the parent and its array-relationship
children written atomically. The library owns the input *shape* (the child row
input + its ``{data: [...]}`` envelope + the optional field on the parent
insert input) and reduces the envelope to plain nested dicts; the caller's
``write_backend`` owns persistence and atomicity. These tests drive one
mutation over the ``Book`` / ``Chapter`` pair and prove parent + children land
together, a child failure rolls the whole write back, and the child input
carries the optional public ``id`` that also drives an upsert diff.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest
import strawberry
import strawberry_django
from django.core.exceptions import FieldDoesNotExist, ValidationError
from django.db import transaction
from strawberry import auto
from strawberry.scalars import JSON

from strawberry_django_hasura import (
    NestedInsert,
    hasura_resource,
    input_to_dict,
)
from tests.models import AuthorModel, BookModel, ChapterModel, TagModel
from tests.test_crud import (
    _input_block,
    decode_author,
    encode_author,
    encode_book,
)


@strawberry_django.type(ChapterModel)
class Chapter:
    title: auto
    position: auto

    @strawberry.field
    def id(self) -> strawberry.ID:
        return strawberry.ID(f"chap{self.pk}")


@strawberry_django.type(BookModel)
class BookWithChapters:
    title: auto

    @strawberry.field
    def id(self) -> strawberry.ID:
        return strawberry.ID(encode_book(self.pk))

    @strawberry_django.field(only=["author_id"])
    def author(self) -> strawberry.ID:
        return strawberry.ID(encode_author(self.author_id))

    @strawberry_django.field
    def chapters(self) -> list[Chapter]:
        return list(self.chapters.order_by("position", "pk"))


class BookWithChaptersWriteBackend:
    """Persist a book and its nested chapters atomically (the caller's seam).

    The library hands ``create`` the reduced nested dict (``{"title": ...,
    "author": ..., "chapters": {"data": [{...}, ...]}}``); this backend writes
    the parent, then each child under the parent FK, inside one transaction.
    ``full_clean`` validates each child (SQLite ignores a ``CharField`` max
    length), so an over-long chapter title raises and rolls the whole insert
    back. ``id`` (present on the shared child input) is ignored on a pure
    insert.
    """

    def create(self, info: strawberry.Info, data: dict[str, Any]) -> BookModel:
        del info
        with transaction.atomic():
            payload = dict(data)
            chapters = payload.pop("chapters", None)
            if "author" in payload:
                payload["author_id"] = decode_author(payload.pop("author"))
            book = BookModel.objects.create(**payload)
            for row in (chapters or {}).get("data", []):
                child = dict(row)
                child.pop("id", None)
                chapter = ChapterModel(book=book, **child)
                chapter.full_clean()
                chapter.save()
            return book

    def update(
        self, info: strawberry.Info, pk: str, data: dict[str, Any]
    ) -> BookModel:  # pragma: no cover - unused here
        raise NotImplementedError

    def delete(
        self, info: strawberry.Info, pk: str
    ) -> BookModel | None:  # pragma: no cover - unused here
        raise NotImplementedError


def _book_with_chapters_resource(**overrides: Any):
    kwargs: dict[str, Any] = {
        "model": BookModel,
        "name": "books",
        "filterable": ["id", "title"],
        "sortable": ["title"],
        "aggregatable": [],
        "writable": ["title", "author"],
        "field_id_decode": {"author": decode_author},
        "nested": [NestedInsert(relation="chapters", model=ChapterModel)],
        "get_queryset": lambda info: BookModel.objects.all(),
        "write_backend": BookWithChaptersWriteBackend(),
        "id_decode": lambda value: int(str(value).removeprefix("book")),
    }
    kwargs.update(overrides)
    return hasura_resource(BookWithChapters, **kwargs)


def _book_with_chapters_schema() -> strawberry.Schema:
    resource = _book_with_chapters_resource()
    return strawberry.Schema(
        query=resource.query,
        mutation=resource.mutation,
        types=[Chapter, *resource.types],
    )


def test_input_to_dict_reduces_nested_envelope_to_plain_dicts():
    """``input_to_dict`` recurses through the ``{data: [...]}`` envelope."""

    @strawberry.input
    class ChildInput:
        title: str
        position: int

    @strawberry.input
    class ArrInput:
        data: list[ChildInput]

    @strawberry.input
    class ParentInput:
        title: str
        chapters: ArrInput

    reduced = input_to_dict(
        ParentInput(
            title="Book",
            chapters=ArrInput(
                data=[
                    ChildInput(title="One", position=0),
                    ChildInput(title="Two", position=1),
                ]
            ),
        )
    )
    assert reduced == {
        "title": "Book",
        "chapters": {
            "data": [
                {"title": "One", "position": 0},
                {"title": "Two", "position": 1},
            ]
        },
    }


def test_nested_insert_sdl_shape():
    """The parent insert input gains the nested envelope; the child input
    carries an optional public ``id`` (the upsert key)."""

    resource = _book_with_chapters_resource()
    schema = strawberry.Schema(
        query=resource.query,
        mutation=resource.mutation,
        types=[Chapter, *resource.types],
    )
    sdl = schema.as_str()

    assert "chapters: books_chapters_arr_rel_insert_input" in _input_block(
        sdl, "books_insert_input"
    )
    arr_block = _input_block(sdl, "books_chapters_arr_rel_insert_input")
    assert "data: [books_chapters_insert_input!]!" in arr_block
    child_body = _input_block(sdl, "books_chapters_insert_input").split(
        "{", 1
    )[1]
    assert "id: String" in child_body
    assert "title: String" in child_body
    assert "position: Int" in child_body
    # The back-FK to the parent is supplied by the nesting, never asked for.
    assert "book:" not in child_body
    # And the built resource exposes the child input for a consumer's upsert.
    assert resource.nested_input_types["chapters"] is not None
    assert resource.nested_arr_input_types["chapters"] is not None


def test_nested_insert_persists_parent_and_children_atomically(db):
    author = AuthorModel.objects.create(name="Ada")
    schema = _book_with_chapters_schema()

    result = schema.execute_sync(
        """
        mutation($object: books_insert_input!) {
          insert_books_one(object: $object) {
            title
            chapters { title position }
          }
        }
        """,
        variable_values={
            "object": {
                "title": "Compiler Design",
                "author": encode_author(author.pk),
                "chapters": {
                    "data": [
                        {"title": "Lexing", "position": 0},
                        {"title": "Parsing", "position": 1},
                    ]
                },
            }
        },
    )
    assert result.errors is None, result.errors
    assert result.data["insert_books_one"] == {
        "title": "Compiler Design",
        "chapters": [
            {"title": "Lexing", "position": 0},
            {"title": "Parsing", "position": 1},
        ],
    }
    book = BookModel.objects.get(title="Compiler Design")
    assert list(
        book.chapters.order_by("position").values_list("title", flat=True)
    ) == ["Lexing", "Parsing"]


def test_nested_insert_rolls_back_parent_on_child_failure(db):
    """A failing child (over-long title) rolls the parent back — no orphan."""

    author = AuthorModel.objects.create(name="Ada")
    schema = _book_with_chapters_schema()

    result = schema.execute_sync(
        """
        mutation($object: books_insert_input!) {
          insert_books_one(object: $object) { title }
        }
        """,
        variable_values={
            "object": {
                "title": "Doomed",
                "author": encode_author(author.pk),
                "chapters": {
                    "data": [
                        {"title": "ok", "position": 0},
                        {"title": "x" * 40, "position": 1},
                    ]
                },
            }
        },
    )
    assert result.errors is not None
    assert isinstance(result.errors[0].original_error, ValidationError)
    assert not BookModel.objects.filter(title="Doomed").exists()
    assert not ChapterModel.objects.filter(title="ok").exists()


def test_input_to_dict_leaves_non_input_values_verbatim():
    """Only a strawberry input reduces — a dataclass-backed scalar value and
    a tuple reach the write backend intact, never flattened or coerced."""

    @dataclasses.dataclass(frozen=True)
    class Money:
        amount: str
        currency: str

    @strawberry.input
    class PriceInput:
        price: JSON
        pair: JSON

    money = Money("9.99", "EUR")
    reduced = input_to_dict(PriceInput(price=money, pair=(1, 2)))
    assert reduced["price"] is money
    assert reduced["pair"] == (1, 2)
    assert isinstance(reduced["pair"], tuple)


def test_nested_insert_spec_is_hashable():
    """The frozen spec stays hashable however the sequence knobs are spelled
    (they freeze to tuples), so specs can key registries / dedupe in sets."""

    spec = NestedInsert(
        relation="chapters",
        model=ChapterModel,
        insertable=["title"],
        public_id_columns=["position"],
    )
    assert isinstance(hash(spec), int)
    assert spec.insertable == ("title",)
    assert spec.public_id_columns == ("position",)


def test_nested_relation_accepts_accessor_and_query_name():
    """Without a ``related_name`` both reverse spellings build: the default
    ``<child>_set`` accessor and the related query name."""

    @strawberry_django.type(AuthorModel)
    class AuthorWithBooks:
        name: auto

        @strawberry.field
        def id(self) -> strawberry.ID:
            return strawberry.ID(encode_author(self.pk))

    for relation in ("bookmodel_set", "bookmodel"):
        resource = hasura_resource(
            AuthorWithBooks,
            model=AuthorModel,
            name="authors",
            filterable=["id"],
            sortable=[],
            aggregatable=[],
            writable=["name"],
            nested=[NestedInsert(relation=relation, model=BookModel)],
            get_queryset=lambda info: AuthorModel.objects.all(),
            write_backend=BookWithChaptersWriteBackend(),
        )
        assert relation in resource.nested_input_types
        # The child's FK back to the parent stays excluded either way.
        child_fields = resource.nested_input_types[relation].__annotations__
        assert "author" not in child_fields
        assert "title" in child_fields


def test_nested_relation_must_exist():
    with pytest.raises(FieldDoesNotExist, match="no relation 'shelves'"):
        _book_with_chapters_resource(
            nested=[NestedInsert(relation="shelves", model=ChapterModel)]
        )


def test_nested_relation_must_be_reverse_to_many():
    """A forward FK is not an array relationship — named build-time error,
    not an AttributeError deep in schema build."""

    with pytest.raises(TypeError, match="not a reverse to-many"):
        _book_with_chapters_resource(
            nested=[NestedInsert(relation="author", model=AuthorModel)]
        )


def test_nested_model_must_match_the_relation_target():
    with pytest.raises(TypeError, match="targets ChapterModel"):
        _book_with_chapters_resource(
            nested=[NestedInsert(relation="chapters", model=TagModel)]
        )


def test_nested_back_fk_in_insertable_fails_fast():
    """The FK back to the parent is supplied by the nesting; allowlisting it
    raises instead of being silently dropped."""

    with pytest.raises(TypeError, match="foreign key back to the parent"):
        _book_with_chapters_resource(
            nested=[
                NestedInsert(
                    relation="chapters",
                    model=ChapterModel,
                    insertable=["book", "title"],
                )
            ]
        )


def test_nested_requires_insert_enabled():
    """``nested=`` only shapes insert inputs — a read-only resource must not
    leak write-shaped child input types into its SDL."""

    with pytest.raises(TypeError, match="requires\\s+insert=True"):
        _book_with_chapters_resource(insert=False, update=False, delete=False)


def test_nested_child_id_column_is_excluded():
    """``NestedInsert(id_column=…)`` marks the child's public-id column as
    server-owned — it never appears as a writable child field."""

    resource = _book_with_chapters_resource(
        nested=[
            NestedInsert(
                relation="chapters",
                model=ChapterModel,
                id_column="position",
            )
        ]
    )
    schema = strawberry.Schema(
        query=resource.query,
        mutation=resource.mutation,
        types=[Chapter, *resource.types],
    )
    child_block = _input_block(schema.as_str(), "books_chapters_insert_input")
    assert "position" not in child_block
    assert "title: String" in child_block


def test_nested_public_id_columns_type_child_columns_as_id():
    resource = _book_with_chapters_resource(
        nested=[
            NestedInsert(
                relation="chapters",
                model=ChapterModel,
                public_id_columns=["position"],
            )
        ]
    )
    schema = strawberry.Schema(
        query=resource.query,
        mutation=resource.mutation,
        types=[Chapter, *resource.types],
    )
    child_block = _input_block(schema.as_str(), "books_chapters_insert_input")
    assert "position: ID" in child_block


def test_reverse_relations_are_never_writable():
    """A reverse accessor (here TagModel's side of ``BookModel.tags``) is the
    other model's relation — excluded from the writable scan instead of
    exposed as a client-settable array (or crashing the input build)."""

    @strawberry_django.type(TagModel)
    class Tag:
        name: auto

    resource = hasura_resource(
        Tag,
        model=TagModel,
        name="tags",
        filterable=[],
        sortable=[],
        aggregatable=[],
        get_queryset=lambda info: TagModel.objects.all(),
        write_backend=BookWithChaptersWriteBackend(),
    )
    assert resource.insertable_fields == ("name",)


def test_nested_null_envelope_means_no_children(db):
    """``chapters: null`` reaches the backend with the key absent (Hasura: a
    null relationship envelope means no children, not a null column)."""

    class CapturingBackend(BookWithChaptersWriteBackend):
        def __init__(self) -> None:
            self.seen: list[dict[str, Any]] = []

        def create(
            self, info: strawberry.Info, data: dict[str, Any]
        ) -> BookModel:
            self.seen.append(dict(data))
            return super().create(info, data)

    author = AuthorModel.objects.create(name="Ada")
    backend = CapturingBackend()
    resource = _book_with_chapters_resource(write_backend=backend)
    schema = strawberry.Schema(
        query=resource.query,
        mutation=resource.mutation,
        types=[Chapter, *resource.types],
    )
    result = schema.execute_sync(
        """
        mutation($object: books_insert_input!) {
          insert_books_one(object: $object) { title }
        }
        """,
        variable_values={
            "object": {
                "title": "Solo",
                "author": encode_author(author.pk),
                "chapters": None,
            }
        },
    )
    assert result.errors is None, result.errors
    assert "chapters" not in backend.seen[0]
    assert BookModel.objects.get(title="Solo").chapters.count() == 0


def test_child_column_named_id_collides_with_upsert_key():
    """A writable child column literally named ``id`` would silently shadow
    the injected public upsert key — it raises instead (unit-level: no test
    model may carry a non-pk ``id`` column without polluting the registry)."""

    import types as _types

    from strawberry_django_hasura.resource import _insert_input_type

    class _IdColumn:
        name = "id"

    with pytest.raises(TypeError, match="collides with the public upsert"):
        _insert_input_type(
            "x_insert_input",
            [_IdColumn()],
            frozenset(),
            _types.ModuleType("tests._x"),
            with_public_id=True,
        )
