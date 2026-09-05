"""The ordinary async GraphQL executor can use every ORM resource root."""

from __future__ import annotations

import asyncio

import pytest
import strawberry
import strawberry_django
from asgiref.sync import sync_to_async
from django.db import connections
from strawberry_django.optimizer import DjangoOptimizerExtension

from strawberry_django_hasura import hasura_resource
from tests.demo_schema import Note, NoteWriteBackend, decode_sqid, seed
from tests.models import NoteModel
from tests.test_optimizer import _build_schema, _seed


def _run_async(awaitable):
    async def run():
        try:
            return await awaitable
        finally:
            # These tests bypass Django's request lifecycle. Close the ORM
            # worker's connection before pytest destroys the test database.
            await sync_to_async(connections.close_all, thread_sensitive=True)()

    return asyncio.run(run())


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("optimizer", [False, True])
def test_async_model_reads_and_mutations(optimizer):
    seed()
    resource = hasura_resource(
        Note,
        model=NoteModel,
        name="async_notes",
        filterable=["id", "title", "status"],
        sortable=["id", "title"],
        aggregatable=["word_count"],
        groupable=["status"],
        get_queryset=lambda info: NoteModel.objects.all(),
        write_backend=NoteWriteBackend(),
        id_decode=decode_sqid,
    )
    schema = strawberry.Schema(
        query=resource.query,
        mutation=resource.mutation,
        types=resource.types,
        extensions=[DjangoOptimizerExtension] if optimizer else [],
    )

    async def exercise():
        reads = await schema.execute(
            """{
              async_notes(limit: 2) {id title}
              async_notes_aggregate {
                aggregate {count sum {word_count}}
                nodes {title}
              }
              async_notes_groups(group_by:[{field:STATUS}]) {
                aggregate {count}
              }
              async_notes_groups_count(group_by:[{field:STATUS}])
            }"""
        )
        assert reads.errors is None, reads.errors
        assert len(reads.data["async_notes"]) == 2
        assert reads.data["async_notes_aggregate"]["aggregate"]["count"] == 3
        assert len(reads.data["async_notes_aggregate"]["nodes"]) == 3
        assert reads.data["async_notes_groups_count"] == 2
        pk = reads.data["async_notes"][0]["id"]
        detail = await schema.execute(
            "query($id:String!){async_notes_by_pk(id:$id){id title}}",
            variable_values={"id": pk},
        )
        assert detail.errors is None, detail.errors
        assert detail.data["async_notes_by_pk"]["id"] == pk
        inserted = await schema.execute(
            'mutation{insert_async_notes_one(object:{title:"Async"}){id}}'
        )
        assert inserted.errors is None, inserted.errors
        new_pk = inserted.data["insert_async_notes_one"]["id"]
        updated = await schema.execute(
            """mutation($id:String!){
              update_async_notes_by_pk(pk_columns:{id:$id},
                                      _set:{title:"Updated"}){title}
            }""",
            variable_values={"id": new_pk},
        )
        assert updated.errors is None, updated.errors
        assert updated.data["update_async_notes_by_pk"]["title"] == "Updated"
        deleted = await schema.execute(
            "mutation($id:String!){delete_async_notes_by_pk(id:$id){id}}",
            variable_values={"id": new_pk},
        )
        assert deleted.errors is None, deleted.errors
        assert deleted.data["delete_async_notes_by_pk"]["id"] == new_pk

    _run_async(exercise())
    assert not NoteModel.objects.filter(title="Updated").exists()


@pytest.mark.django_db(transaction=True)
def test_async_list_and_aggregate_nodes_keep_related_fields_usable():
    schema = _build_schema()
    _seed(3)

    async def exercise():
        result = await schema.execute(
            """{
              books(limit: 2) {title author{name} tags{name}}
              books_aggregate {nodes {title author{name} tags{name}}}
            }"""
        )
        assert result.errors is None, result.errors
        assert len(result.data["books"]) == 2
        assert len(result.data["books_aggregate"]["nodes"]) == 3
        assert len(result.data["books"][0]["tags"]) == 2

    _run_async(exercise())


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("asynchronous", [False, True])
def test_root_scope_is_not_reapplied_by_node_hook_after_pagination(
    asynchronous,
):
    seed()
    node_scope_calls = 0

    @strawberry_django.type(NoteModel)
    class ScopedNote:
        title: strawberry.auto

        @classmethod
        def get_queryset(cls, queryset, info):
            nonlocal node_scope_calls
            node_scope_calls += 1
            return queryset.filter(status="published")

    resource = hasura_resource(
        ScopedNote,
        model=NoteModel,
        name="scoped_notes",
        filterable=["title"],
        sortable=["title"],
        aggregatable=[],
        get_queryset=lambda info: NoteModel.objects.filter(status="published"),
        max_rows=1,
        write_backend=NoteWriteBackend(),
        insert=False,
        update=False,
        delete=False,
    )
    schema = strawberry.Schema(
        query=resource.query,
        types=resource.types,
        extensions=[DjangoOptimizerExtension],
    )
    document = """{
      scoped_notes(limit: 1) {title}
      scoped_notes_aggregate {nodes {title} aggregate {count}}
    }"""
    result = (
        _run_async(schema.execute(document))
        if asynchronous
        else schema.execute_sync(document)
    )
    assert result.errors is None, result.errors
    assert result.data["scoped_notes"] == [{"title": "Alpha"}]
    assert result.data["scoped_notes_aggregate"] == {
        "nodes": [{"title": "Alpha"}],
        "aggregate": {"count": 2},
    }
    assert node_scope_calls == 0
