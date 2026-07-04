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

from typing import Any

import strawberry
import strawberry_django
from django.core.exceptions import ValidationError
from django.db import transaction
from strawberry import auto

from strawberry_django_hasura import (
    NestedInsert,
    hasura_resource,
    input_to_dict,
)
from tests.models import AuthorModel, BookModel, ChapterModel
from tests.test_crud import decode_author, encode_author, encode_book


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


def _book_with_chapters_resource():
    return hasura_resource(
        BookWithChapters,
        model=BookModel,
        name="books",
        filterable=["id", "title"],
        sortable=["title"],
        aggregatable=[],
        writable=["title", "author"],
        field_id_decode={"author": decode_author},
        nested=[NestedInsert(relation="chapters", model=ChapterModel)],
        get_queryset=lambda info: BookModel.objects.all(),
        write_backend=BookWithChaptersWriteBackend(),
        id_decode=lambda value: int(str(value).removeprefix("book")),
    )


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


def _input_block(sdl: str, name: str) -> str:
    start = sdl.index(f"input {name} {{")
    end = sdl.index("}", start)
    return sdl[start:end]
