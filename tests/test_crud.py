"""Runtime CRUD — list (where/order_by/limit/offset), by-pk, insert/update/del.

Converted from the spike's ``client_test_hasura.cjs`` runtime section: seeds
an in-memory table and executes each operation the stock provider drives,
asserting the results and the sqid roundtrip through the pk-centric ops.
"""

from __future__ import annotations

import datetime
from typing import Any

import strawberry
import strawberry_django
from strawberry import auto
from strawberry.types import get_object_definition

from strawberry_django_hasura import hasura_resource, paginate
from strawberry_django_hasura.resource import _pin_snake_wire_names
from tests.demo_schema import (
    Note,
    NoteWriteBackend,
    decode_sqid,
    encode_sqid,
    get_queryset,
)
from tests.models import AuthorModel, BookModel, TagModel


def encode_author(pk: int) -> str:
    return f"auth{pk}"


def decode_author(value: str) -> int:
    return int(str(value).removeprefix("auth"))


def encode_book(pk: int) -> str:
    return f"book{pk}"


def decode_book(value: str) -> int:
    return int(str(value).removeprefix("book"))


def encode_tag(pk: int) -> str:
    return f"tag{pk}"


def decode_tag(value: str) -> int:
    return int(str(value).removeprefix("tag"))


@strawberry_django.type(AuthorModel)
class Author:
    name: auto

    @strawberry.field
    def id(self) -> strawberry.ID:
        return strawberry.ID(encode_author(self.pk))


@strawberry_django.type(BookModel)
class Book:
    title: auto
    updated_at: auto

    @strawberry.field
    def id(self) -> strawberry.ID:
        return strawberry.ID(encode_book(self.pk))

    @strawberry_django.field(only=["author_id"])
    def author(self) -> strawberry.ID:
        return strawberry.ID(encode_author(self.author_id))

    @strawberry_django.field
    def tags(self) -> list[strawberry.ID]:
        return [
            strawberry.ID(encode_tag(pk))
            for pk in self.tags.order_by("pk").values_list("pk", flat=True)
        ]


@strawberry.type
class RecursiveNode:
    display_name: str
    related_nodes: list[RecursiveNode]


class BookWriteBackend:
    def create(self, info: strawberry.Info, data: dict[str, Any]) -> BookModel:
        del info
        decoded = _book_write_data(data)
        tags = decoded.pop("tags", None)
        obj = BookModel.objects.create(**decoded)
        if tags is not None:
            obj.tags.set(decode_tag(tag) for tag in tags)
        return obj

    def update(
        self,
        info: strawberry.Info,
        pk: str,
        data: dict[str, Any],
    ) -> BookModel:
        del info
        obj = BookModel.objects.get(pk=decode_book(pk))
        decoded = _book_write_data(data)
        tags = decoded.pop("tags", None)
        for key, value in decoded.items():
            setattr(obj, key, value)
        obj.save(update_fields=[*decoded, "updated_at"])
        if tags is not None:
            obj.tags.set(decode_tag(tag) for tag in tags)
        return obj

    def delete(self, info: strawberry.Info, pk: str) -> BookModel | None:
        del info
        obj = BookModel.objects.filter(pk=decode_book(pk)).first()
        if obj is None:
            return None
        deleted_pk = obj.pk
        obj.delete()
        obj.pk = deleted_pk
        return obj


def _book_write_data(data: dict[str, Any]) -> dict[str, Any]:
    out = dict(data)
    if "author" in out:
        out["author_id"] = decode_author(out.pop("author"))
    return out


def _book_resource(**kwargs: Any):
    field_id_decode = {
        "author": decode_author,
        **kwargs.pop("field_id_decode", {}),
    }
    return hasura_resource(
        Book,
        model=BookModel,
        name="books",
        filterable=["id", "title", "author"],
        sortable=["title", "updated_at"],
        aggregatable=[],
        writable=kwargs.pop("writable", ["title", "author"]),
        get_queryset=lambda info: BookModel.objects.all(),
        write_backend=BookWriteBackend(),
        id_decode=decode_book,
        field_id_decode=field_id_decode,
        **kwargs,
    )


def test_paginate_adds_pk_tiebreaker_when_unordered(seeded_notes):
    """Offset paging over an unordered queryset gets a deterministic ``pk``
    tiebreaker so pages don't overlap or skip rows."""
    qs = seeded_notes.objects.all()
    assert not qs.ordered
    paged = paginate(qs, 2, 0)
    assert paged.query.order_by == ("pk",)


def test_paginate_keeps_caller_ordering(seeded_notes):
    """A caller-supplied ordering is left untouched."""
    paged = paginate(seeded_notes.objects.order_by("title"), 2, None)
    assert paged.query.order_by == ("title",)


def test_snake_name_pinning_handles_recursive_node_graph():
    """Nested snake-name pinning terminates on cyclic object graphs."""

    _pin_snake_wire_names(RecursiveNode)
    definition = get_object_definition(RecursiveNode)
    assert definition is not None
    fields = {
        field.python_name: field.graphql_name for field in definition.fields
    }
    assert fields["display_name"] == "display_name"
    assert fields["related_nodes"] == "related_nodes"


def test_list_where_order_limit(schema, seeded_notes):
    result = schema.execute_sync(
        """query($w: notes_bool_exp, $o: [notes_order_by!],
                 $l: Int, $off: Int){
             notes(where:$w, order_by:$o, limit:$l, offset:$off){
               id title word_count status } }""",
        variable_values={
            "w": {"status": {"_eq": "published"}},
            "o": [{"word_count": "desc"}],
            "l": 10,
            "off": 0,
        },
    )
    assert result.errors is None, result.errors
    rows = result.data["notes"]
    # where status=published narrows to Alpha + Cee; order desc → Cee, Alpha.
    assert [n["title"] for n in rows] == ["Cee", "Alpha"]
    # ids are sqids (sq*), not raw pks.
    assert all(str(n["id"]).startswith("sq") for n in rows)


def test_list_where_by_sqid_id(schema, seeded_notes):
    """``where: { id: { _eq: "<sqid>" } }`` decodes the sqid before the pk
    lookup (the spike's getList-by-id check)."""
    first = seeded_notes.objects.order_by("pk").first()
    sqid = encode_sqid(first.pk)
    result = schema.execute_sync(
        "query($w: notes_bool_exp){ notes(where:$w){ id title } }",
        variable_values={"w": {"id": {"_eq": sqid}}},
    )
    assert result.errors is None, result.errors
    rows = result.data["notes"]
    assert len(rows) == 1
    assert rows[0]["id"] == sqid
    assert rows[0]["title"] == first.title


def test_string_ilike(schema, seeded_notes):
    result = schema.execute_sync(
        "query($w: notes_bool_exp){ notes(where:$w){ title } }",
        variable_values={"w": {"title": {"_ilike": "a"}}},
    )
    assert result.errors is None, result.errors
    # "Alpha" and "Bravo" both contain a case-insensitive "a".
    assert len(result.data["notes"]) == 2


def test_by_pk(schema, seeded_notes):
    first = seeded_notes.objects.order_by("pk").first()
    sqid = encode_sqid(first.pk)
    result = schema.execute_sync(
        "query($id: String!){ notes_by_pk(id:$id){ id title } }",
        variable_values={"id": sqid},
    )
    assert result.errors is None, result.errors
    assert result.data["notes_by_pk"]["id"] == sqid
    assert result.data["notes_by_pk"]["title"] == first.title


def test_insert_one_persists(schema, seeded_notes):
    result = schema.execute_sync(
        """mutation($o: notes_insert_input!){
             insert_notes_one(object:$o){ id title word_count } }""",
        variable_values={"o": {"title": "Delta", "word_count": 5}},
    )
    assert result.errors is None, result.errors
    assert seeded_notes.objects.filter(title="Delta").exists()
    created = result.data["insert_notes_one"]
    assert created["word_count"] == 5
    assert str(created["id"]).startswith("sq")


def test_update_by_pk_patches_only_set_fields(schema, seeded_notes):
    note = seeded_notes.objects.create(
        title="Echo", word_count=1, status="draft"
    )
    sqid = encode_sqid(note.pk)
    result = schema.execute_sync(
        """mutation($pk: notes_pk_columns_input!, $s: notes_set_input!){
             update_notes_by_pk(pk_columns:$pk, _set:$s){
               id word_count status } }""",
        variable_values={"pk": {"id": sqid}, "s": {"word_count": 99}},
    )
    assert result.errors is None, result.errors
    assert result.data["update_notes_by_pk"]["id"] == sqid
    note.refresh_from_db()
    assert note.word_count == 99
    # An omitted field is not clobbered by the patch.
    assert note.status == "draft"


def test_update_by_pk_refreshes_auto_now(schema, seeded_notes):
    """A partial patch still bumps the ``auto_now`` ``updated_at`` (added to
    ``update_fields``, which Django otherwise skips for unlisted columns)."""
    note = seeded_notes.objects.create(title="Golf", word_count=1)
    old = datetime.datetime(2000, 1, 1, tzinfo=datetime.UTC)
    seeded_notes.objects.filter(pk=note.pk).update(updated_at=old)
    result = schema.execute_sync(
        """mutation($pk: notes_pk_columns_input!, $s: notes_set_input!){
             update_notes_by_pk(pk_columns:$pk, _set:$s){ word_count } }""",
        variable_values={
            "pk": {"id": encode_sqid(note.pk)},
            "s": {"word_count": 7},
        },
    )
    assert result.errors is None, result.errors
    note.refresh_from_db()
    assert note.word_count == 7
    assert note.updated_at > old


def test_delete_by_pk_removes(schema, seeded_notes):
    note = seeded_notes.objects.create(title="Foxtrot", word_count=1)
    sqid = encode_sqid(note.pk)
    result = schema.execute_sync(
        "mutation($id: String!){ delete_notes_by_pk(id:$id){ id title } }",
        variable_values={"id": sqid},
    )
    assert result.errors is None, result.errors
    assert not seeded_notes.objects.filter(pk=note.pk).exists()
    # The response still resolves the deleted record's sqid id + fields.
    assert result.data["delete_notes_by_pk"]["id"] == sqid
    assert result.data["delete_notes_by_pk"]["title"] == "Foxtrot"


def test_sqid_roundtrip_is_reversible():
    assert decode_sqid(encode_sqid(42)) == 42


def test_writable_allowlist_shapes_insert_and_set_inputs(seeded_notes):
    resource = hasura_resource(
        Note,
        model=seeded_notes,
        name="write_notes",
        filterable=["id", "title", "word_count", "status"],
        sortable=["title"],
        aggregatable=["word_count"],
        writable=["title"],
        get_queryset=get_queryset,
        write_backend=NoteWriteBackend(),
        id_decode=decode_sqid,
    )
    write_schema = strawberry.Schema(
        query=resource.query,
        mutation=resource.mutation,
        types=resource.types,
    )
    sdl = write_schema.as_str()

    insert_block = _input_block(sdl, "write_notes_insert_input")
    set_block = _input_block(sdl, "write_notes_set_input")
    assert "title:" in insert_block
    assert "title:" in set_block
    assert "word_count" not in insert_block
    assert "word_count" not in set_block
    assert "status" not in insert_block
    assert "status" not in set_block

    result = write_schema.execute_sync(
        """mutation($o: write_notes_insert_input!){
          insert_write_notes_one(object: $o) { title word_count }
        }""",
        variable_values={"o": {"title": "Writable only"}},
    )
    assert result.errors is None, result.errors
    assert result.data["insert_write_notes_one"] == {
        "title": "Writable only",
        "word_count": 0,
    }


def test_mutation_operations_can_be_disabled(seeded_notes):
    resource = hasura_resource(
        Note,
        model=seeded_notes,
        name="patch_notes",
        filterable=["id", "title", "word_count", "status"],
        sortable=["title"],
        aggregatable=["word_count"],
        writable=["title"],
        insert=False,
        delete=False,
        get_queryset=get_queryset,
        write_backend=NoteWriteBackend(),
        id_decode=decode_sqid,
    )
    write_schema = strawberry.Schema(
        query=resource.query,
        mutation=resource.mutation,
        types=resource.types,
    )
    sdl = write_schema.as_str()

    assert "update_patch_notes_by_pk" in sdl
    assert "patch_notes_set_input" in sdl
    assert "patch_notes_pk_columns_input" in sdl
    assert "insert_patch_notes_one" not in sdl
    assert "delete_patch_notes_by_pk" not in sdl
    assert "patch_notes_insert_input" not in sdl


def test_insert_and_update_allowlists_can_differ(seeded_notes):
    resource = hasura_resource(
        Note,
        model=seeded_notes,
        name="split_notes",
        filterable=["id", "title", "word_count", "status"],
        sortable=["title"],
        aggregatable=["word_count"],
        insertable=["title"],
        updatable=["title", "status"],
        get_queryset=get_queryset,
        write_backend=NoteWriteBackend(),
        id_decode=decode_sqid,
    )
    write_schema = strawberry.Schema(
        query=resource.query,
        mutation=resource.mutation,
        types=resource.types,
    )
    sdl = write_schema.as_str()

    insert_block = _input_block(sdl, "split_notes_insert_input")
    set_block = _input_block(sdl, "split_notes_set_input")
    assert "title:" in insert_block
    assert "status" not in insert_block
    assert "title:" in set_block
    assert "status:" in set_block


def test_fk_public_id_fields_filter_and_write(db):
    author = AuthorModel.objects.create(name="Ada")
    other = AuthorModel.objects.create(name="Grace")
    BookModel.objects.create(title="Compiler Notes", author=author)
    BookModel.objects.create(title="Systems Notes", author=other)
    resource = _book_resource()
    book_schema = strawberry.Schema(
        query=resource.query,
        mutation=resource.mutation,
        types=[Author, *resource.types],
    )
    sdl = book_schema.as_str()

    assert "author: ID!" in _input_block(sdl, "books_insert_input")
    assert "author: ID" in _input_block(sdl, "books_set_input")

    listed = book_schema.execute_sync(
        """
        query($where: books_bool_exp) {
          books(where: $where) { title author }
        }
        """,
        variable_values={
            "where": {"author": {"_eq": encode_author(author.pk)}}
        },
    )
    assert listed.errors is None, listed.errors
    assert listed.data["books"] == [
        {"title": "Compiler Notes", "author": encode_author(author.pk)}
    ]

    inserted = book_schema.execute_sync(
        """
        mutation($object: books_insert_input!) {
          insert_books_one(object: $object) { title author }
        }
        """,
        variable_values={
            "object": {
                "title": "New Systems",
                "author": encode_author(other.pk),
            }
        },
    )
    assert inserted.errors is None, inserted.errors
    assert inserted.data["insert_books_one"] == {
        "title": "New Systems",
        "author": encode_author(other.pk),
    }
    assert BookModel.objects.filter(title="New Systems", author=other).exists()


def test_nested_boolean_filter_decodes_fk_public_id(db):
    """A public-id FK filter nested under ``_and`` decodes its operand too.

    ``field_decoders`` must propagate through the ``_and`` / ``_or`` / ``_not``
    recursion — otherwise the raw sqid reaches the ORM lookup and the filter
    mis-matches (or errors), silently widening a permission-naive read.
    """
    author = AuthorModel.objects.create(name="Ada")
    other = AuthorModel.objects.create(name="Grace")
    BookModel.objects.create(title="Compiler Notes", author=author)
    BookModel.objects.create(title="Systems Notes", author=other)
    resource = _book_resource()
    book_schema = strawberry.Schema(
        query=resource.query,
        mutation=resource.mutation,
        types=[Author, *resource.types],
    )

    listed = book_schema.execute_sync(
        """
        query($where: books_bool_exp) {
          books(where: $where) { title author }
        }
        """,
        variable_values={
            "where": {"_and": [{"author": {"_eq": encode_author(author.pk)}}]}
        },
    )
    assert listed.errors is None, listed.errors
    assert listed.data["books"] == [
        {"title": "Compiler Notes", "author": encode_author(author.pk)}
    ]


def test_many_to_many_public_id_fields_write_relation_arrays(db):
    author = AuthorModel.objects.create(name="Ada")
    alpha = TagModel.objects.create(name="alpha")
    beta = TagModel.objects.create(name="beta")
    gamma = TagModel.objects.create(name="gamma")
    book = BookModel.objects.create(title="Compiler Notes", author=author)
    book.tags.add(gamma)
    resource = _book_resource(
        writable=["title", "author", "tags"],
        field_id_decode={"tags": decode_tag},
    )
    book_schema = strawberry.Schema(
        query=resource.query,
        mutation=resource.mutation,
        types=[Author, *resource.types],
    )
    sdl = book_schema.as_str()

    assert "tags: [ID!]" in _input_block(sdl, "books_insert_input")
    assert "tags: [ID!]" in _input_block(sdl, "books_set_input")

    inserted = book_schema.execute_sync(
        """
        mutation($object: books_insert_input!) {
          insert_books_one(object: $object) { title tags }
        }
        """,
        variable_values={
            "object": {
                "title": "Tagged Systems",
                "author": encode_author(author.pk),
                "tags": [encode_tag(alpha.pk), encode_tag(beta.pk)],
            }
        },
    )
    assert inserted.errors is None, inserted.errors
    assert inserted.data["insert_books_one"] == {
        "title": "Tagged Systems",
        "tags": [encode_tag(alpha.pk), encode_tag(beta.pk)],
    }

    updated = book_schema.execute_sync(
        """
        mutation($pk: books_pk_columns_input!, $set: books_set_input!) {
          update_books_by_pk(pk_columns: $pk, _set: $set) { title tags }
        }
        """,
        variable_values={
            "pk": {"id": encode_book(book.pk)},
            "set": {"tags": [encode_tag(beta.pk)]},
        },
    )
    assert updated.errors is None, updated.errors
    assert updated.data["update_books_by_pk"] == {
        "title": "Compiler Notes",
        "tags": [encode_tag(beta.pk)],
    }
    assert list(book.tags.order_by("pk").values_list("pk", flat=True)) == [
        beta.pk
    ]


def test_groups_support_relation_path_dimensions(db):
    author = AuthorModel.objects.create(name="Ada")
    other = AuthorModel.objects.create(name="Grace")
    BookModel.objects.create(title="A", author=author)
    BookModel.objects.create(title="B", author=author)
    BookModel.objects.create(title="C", author=other)
    resource = _book_resource(groupable=["author__name"])
    book_schema = strawberry.Schema(
        query=resource.query,
        mutation=resource.mutation,
        types=resource.types,
    )

    result = book_schema.execute_sync(
        """
        query {
          books_groups(group_by: [{ field: AUTHOR__NAME }]) {
            key { author__name }
            aggregate { count }
          }
        }
        """
    )

    assert result.errors is None, result.errors
    counts = {
        group["key"]["author__name"]: group["aggregate"]["count"]
        for group in result.data["books_groups"]
    }
    assert counts == {"Ada": 2, "Grace": 1}


def test_groups_max_groups_caps_the_offset_page(db):
    """`hasura_resource(max_groups=...)` bounds an otherwise-unbounded grouped
    read — two distinct authors, but the page is capped to one group."""
    author = AuthorModel.objects.create(name="Ada")
    other = AuthorModel.objects.create(name="Grace")
    BookModel.objects.create(title="A", author=author)
    BookModel.objects.create(title="B", author=other)
    resource = _book_resource(groupable=["author__name"], max_groups=1)
    book_schema = strawberry.Schema(
        query=resource.query,
        mutation=resource.mutation,
        types=[Author, *resource.types],
    )

    result = book_schema.execute_sync(
        """
        query {
          books_groups(group_by: [{ field: AUTHOR__NAME }]) {
            key { author__name }
          }
        }
        """
    )
    assert result.errors is None, result.errors
    assert len(result.data["books_groups"]) == 1


def _input_block(sdl: str, name: str) -> str:
    start = sdl.index(f"input {name} {{")
    end = sdl.index("}", start)
    return sdl[start:end]
