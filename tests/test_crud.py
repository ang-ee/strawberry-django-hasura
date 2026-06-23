"""Runtime CRUD — list (where/order_by/limit/offset), by-pk, insert/update/del.

Converted from the spike's ``client_test_hasura.cjs`` runtime section: seeds
an in-memory table and executes each operation the stock provider drives,
asserting the results and the sqid roundtrip through the pk-centric ops.
"""

from __future__ import annotations

import datetime

from strawberry_django_hasura import paginate
from tests.demo_schema import decode_sqid, encode_sqid


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
