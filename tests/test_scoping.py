"""Consumer scope and write authorization cover every resource root."""

from __future__ import annotations

from typing import Any

import pytest
import strawberry
import strawberry_django
from django.db import transaction

from strawberry_django_hasura import hasura_resource
from tests.models import NoteModel


@strawberry_django.type(NoteModel)
class TenantNote:
    id: strawberry.auto
    title: strawberry.auto
    word_count: strawberry.auto = strawberry.field(name="word_count")


def _scope(info: strawberry.Info):
    # The toy model's status column is this consumer's tenant discriminator.
    return NoteModel.objects.filter(status=info.context["tenant"])


class TenantWrites:
    """Application authorization remains outside the adapter."""

    def create(self, info: strawberry.Info, data: dict[str, Any]):
        return NoteModel.objects.create(status=info.context["tenant"], **data)

    def _authorized(self, info: strawberry.Info, pk: str):
        row = _scope(info).select_for_update().filter(pk=pk).first()
        if row is None:
            raise PermissionError("Note unavailable")
        return row

    def update(self, info: strawberry.Info, pk: str, data: dict[str, Any]):
        with transaction.atomic():
            row = self._authorized(info, pk)
            for key, value in data.items():
                setattr(row, key, value)
            row.save(update_fields=[*data, "updated_at"])
            return row

    def delete(self, info: strawberry.Info, pk: str):
        with transaction.atomic():
            row = self._authorized(info, pk)
            deleted_pk = row.pk
            row.delete()
            row.pk = deleted_pk
            return row


@pytest.fixture
def tenant_resource(db):
    own = NoteModel.objects.create(title="Own", status="a", word_count=10)
    NoteModel.objects.create(title="Also own", status="a", word_count=20)
    other = NoteModel.objects.create(title="Other", status="b", word_count=900)

    def aggregate_scope(info: strawberry.Info):
        # A separately configured aggregate callback must carry the same scope.
        return NoteModel.objects.filter(status=info.context["tenant"])

    resource = hasura_resource(
        TenantNote,
        model=NoteModel,
        name="tenant_notes",
        filterable=["id", "title"],
        sortable=["id"],
        aggregatable=["word_count"],
        groupable=["word_count"],
        writable=["title", "word_count"],
        get_queryset=_scope,
        get_aggregate_queryset=aggregate_scope,
        write_backend=TenantWrites(),
    )
    schema = strawberry.Schema(
        query=resource.query,
        mutation=resource.mutation,
        types=resource.types,
    )
    return schema, own, other


def test_all_read_roots_preserve_consumer_tenant_scope(tenant_resource):
    schema, own, other = tenant_resource
    result = schema.execute_sync(
        """query($own:String!, $other:String!) {
          tenant_notes(where:{_or:[{id:{_eq:$own}},{id:{_eq:$other}}]}) {
            title
          }
          own: tenant_notes_by_pk(id:$own) {title}
          other: tenant_notes_by_pk(id:$other) {title}
          tenant_notes_aggregate {
            aggregate {count sum {word_count}}
            nodes {title}
          }
          denied_aggregate: tenant_notes_aggregate(where:{id:{_eq:$other}}) {
            aggregate {count}
            nodes {title}
          }
          tenant_notes_groups(group_by:[{field:WORD_COUNT}]) {
            key {word_count}
            aggregate {count}
          }
          denied_groups: tenant_notes_groups(
            group_by:[{field:WORD_COUNT}], where:{id:{_eq:$other}}
          ) {aggregate {count}}
          tenant_notes_groups_count(group_by:[{field:WORD_COUNT}])
          denied_count: tenant_notes_groups_count(
            group_by:[{field:WORD_COUNT}], where:{id:{_eq:$other}}
          )
        }""",
        variable_values={"own": str(own.pk), "other": str(other.pk)},
        context_value={"tenant": "a"},
    )
    assert result.errors is None, result.errors
    assert result.data["tenant_notes"] == [{"title": "Own"}]
    assert result.data["own"] == {"title": "Own"}
    assert result.data["other"] is None
    aggregate = result.data["tenant_notes_aggregate"]
    assert aggregate["aggregate"] == {
        "count": 2,
        "sum": {"word_count": "30"},
    }
    assert sorted(row["title"] for row in aggregate["nodes"]) == [
        "Also own",
        "Own",
    ]
    assert result.data["denied_aggregate"] == {
        "aggregate": {"count": 0},
        "nodes": [],
    }
    assert sorted(
        row["key"]["word_count"] for row in result.data["tenant_notes_groups"]
    ) == [10, 20]
    assert result.data["denied_groups"] == []
    assert result.data["tenant_notes_groups_count"] == 2
    assert result.data["denied_count"] == 0


@pytest.mark.parametrize("operation", ["update", "delete"])
def test_write_backend_denies_cross_tenant_ids(tenant_resource, operation):
    schema, own, other = tenant_resource
    if operation == "update":
        document = """mutation($id:String!) {
          update_tenant_notes_by_pk(
            pk_columns:{id:$id}, _set:{title:"Updated"}
          ) {id title}
        }"""
    else:
        document = """mutation($id:String!) {
          delete_tenant_notes_by_pk(id:$id) {id title}
        }"""
    before = list(NoteModel.objects.order_by("pk").values())
    denied = schema.execute_sync(
        document,
        variable_values={"id": str(other.pk)},
        context_value={"tenant": "a"},
    )
    assert denied.errors
    assert denied.errors[0].message == "Note unavailable"
    assert list(NoteModel.objects.order_by("pk").values()) == before
    other.refresh_from_db()
    assert (other.title, other.word_count, other.status) == ("Other", 900, "b")
    allowed = schema.execute_sync(
        document,
        variable_values={"id": str(own.pk)},
        context_value={"tenant": "a"},
    )
    assert allowed.errors is None, allowed.errors
    if operation == "update":
        own.refresh_from_db()
        assert (own.title, own.status) == ("Updated", "a")
    else:
        assert not NoteModel.objects.filter(pk=own.pk).exists()
    other.refresh_from_db()
    assert (other.title, other.status) == ("Other", "b")


def test_create_assigns_tenant_in_backend_and_is_visible_only_there(
    tenant_resource,
):
    schema, _, _ = tenant_resource
    created = schema.execute_sync(
        'mutation{insert_tenant_notes_one(object:{title:"Created"}){id}}',
        context_value={"tenant": "a"},
    )
    assert created.errors is None, created.errors
    pk = created.data["insert_tenant_notes_one"]["id"]
    assert NoteModel.objects.get(pk=pk).status == "a"
    denied = schema.execute_sync(
        "query($id:String!){tenant_notes_by_pk(id:$id){title}}",
        variable_values={"id": pk},
        context_value={"tenant": "b"},
    )
    assert denied.errors is None, denied.errors
    assert denied.data["tenant_notes_by_pk"] is None
