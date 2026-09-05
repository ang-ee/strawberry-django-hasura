"""ORM roots respect the native optimizer active for this execution."""

from contextlib import nullcontext

import pytest
import strawberry
import strawberry_django
from django.db.models import Value
from strawberry_django.optimizer import DjangoOptimizerExtension

from strawberry_django_hasura import hasura_resource
from tests.models import BookModel
from tests.test_async import _run_async
from tests.test_optimizer import BookType, _NoWrites, _seed

_ROOTS = {
    "books": "books(limit: 2)",
    "books_by_pk": "books_by_pk(id: $id)",
    "nodes": "books_aggregate { nodes",
}


@strawberry_django.type(BookModel)
class ExtensionBookType(BookType):
    @strawberry.field
    def extension_marker(self) -> bool:
        return getattr(self, "active_optimizer_marker", False)


def _schema(extension, node_type=BookType):
    resource = hasura_resource(
        node_type,
        model=BookModel,
        name="books",
        filterable=["title"],
        sortable=["title"],
        aggregatable=[],
        get_queryset=lambda info: BookModel.objects.all(),
        write_backend=_NoWrites(),
        insert=False,
        update=False,
        delete=False,
    )
    return strawberry.Schema(
        query=resource.query,
        types=resource.types,
        extensions=[extension] if extension is not None else [],
    )


def _execute(schema, root, asynchronous, *, marker=False):
    fields = "title author { name } tags { name }"
    selection = "{" + fields + (" extensionMarker" if marker else "") + "}"
    arguments = "($id: String!)" if root == "books_by_pk" else ""
    document = f"query{arguments} {{ {_ROOTS[root]} {selection} }}"
    if root == "nodes":
        document += "}"
    variables = (
        {"id": str(BookModel.objects.get(title="Book 0").pk)}
        if root == "books_by_pk"
        else None
    )
    result = (
        _run_async(schema.execute(document, variable_values=variables))
        if asynchronous
        else schema.execute_sync(document, variable_values=variables)
    )
    assert result.errors is None, result.errors
    rows = (
        result.data["books_aggregate"]["nodes"]
        if root == "nodes"
        else result.data[root]
    )
    return [rows] if root == "books_by_pk" else rows


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("root", _ROOTS)
def test_active_extension_runs_before_materialization(root, asynchronous):
    """A consumer annotation reaches resolved model rows in every root."""
    _seed(2)
    calls = []

    class AnnotatingOptimizer(DjangoOptimizerExtension):
        def optimize(self, qs, info, *, store=None):
            if qs.model is BookModel:
                assert qs._result_cache is None
                calls.append(info.field_name)
                qs = qs.annotate(active_optimizer_marker=Value(True))
            return super().optimize(qs, info=info, store=store)

    rows = _execute(
        _schema(AnnotatingOptimizer, ExtensionBookType),
        root,
        asynchronous,
        marker=True,
    )
    assert calls == [root]
    assert rows == [
        {
            "title": f"Book {i}",
            "author": {"name": f"Author {i}"},
            "tags": [
                {"name": f"tag-{i}-a"},
                {"name": f"tag-{i}-b"},
            ],
            "extensionMarker": True,
        }
        for i in range(1 if root == "books_by_pk" else 2)
    ]


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("root", _ROOTS)
@pytest.mark.parametrize("disabled", [False, True])
def test_active_extension_options_are_not_bypassed(
    root, asynchronous, disabled
):
    _seed(2)
    plans = []

    class ConfiguredOptimizer(DjangoOptimizerExtension):
        def __init__(self, **kwargs):
            super().__init__(
                enable_select_related_optimization=disabled,
                enable_prefetch_related_optimization=disabled,
                **kwargs,
            )

        def optimize(self, qs, info, *, store=None):
            result = super().optimize(qs, info=info, store=store)
            if qs.model is BookModel:
                assert qs._result_cache is None
                plans.append(
                    (
                        result.query.select_related,
                        result._prefetch_related_lookups,
                    )
                )
            return result

    schema = _schema(ConfiguredOptimizer)
    context = (
        DjangoOptimizerExtension.disabled() if disabled else nullcontext()
    )
    with context:
        rows = _execute(schema, root, asynchronous)
    assert all(len(row["tags"]) == 2 for row in rows)
    assert plans == [(False, ())]


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("root", _ROOTS)
def test_without_active_extension_reads_keep_native_fallback(
    root, asynchronous
):
    _seed(2)
    rows = _execute(_schema(None), root, asynchronous)
    assert len(rows) == (1 if root == "books_by_pk" else 2)
    assert all(len(row["tags"]) == 2 for row in rows)
