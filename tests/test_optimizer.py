"""Regression guard: the Hasura read resolvers lean on strawberry-django's
``DjangoOptimizerExtension`` instead of N+1-ing nested relations.

The adapter does not reimplement query optimization — it hands the optimizer a
lazy queryset and lets ``select_related`` / ``prefetch_related`` / ``.only()``
do their job. Two ways that hand-off can break, both guarded here:

- **list / nodes** return the lazy queryset; the extension auto-optimizes it.
  Re-introducing ``list(...)`` evaluates the queryset before the optimizer's
  ``_result_cache is None`` gate, so nested loads scale with the row count.
  Guard: the query count is CONSTANT across page sizes (the N+1 signature is a
  *growing* count).
- **by_pk** evaluates eagerly via ``.first()``, so it must compose
  ``optimize()`` itself. Dropping that call turns the FK into a second SELECT.
  Guard: the to-one relation is folded into the row's own query (a JOIN), so
  ``books_by_pk { author { ... } tags { ... } }`` is exactly two queries.

Both assertions carry the captured SQL so a regression is diagnosable.
"""

from __future__ import annotations

from typing import Any

import pytest
import strawberry
import strawberry_django
from django.db import connection
from django.test.utils import CaptureQueriesContext
from strawberry import auto
from strawberry_django.optimizer import DjangoOptimizerExtension

from strawberry_django_hasura import hasura_resource
from tests.models import AuthorModel, BookModel, TagModel


@strawberry_django.type(AuthorModel)
class AuthorType:
    name: auto


@strawberry_django.type(TagModel)
class TagType:
    name: auto


@strawberry_django.type(BookModel)
class BookType:
    title: auto
    author: AuthorType
    tags: list[TagType]


class _NoWrites:
    """Stub write seam; these are read-only resources (mutations disabled)."""

    def create(self, info: Any, data: dict[str, Any]) -> Any: ...
    def update(self, info: Any, pk: str, data: dict[str, Any]) -> Any: ...
    def delete(self, info: Any, pk: str) -> Any: ...


def _build_schema() -> strawberry.Schema:
    resource = hasura_resource(
        BookType,
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
        extensions=[DjangoOptimizerExtension],
    )


def _seed(n: int) -> None:
    """N books, each with its own author and two tags (max nested fan-out)."""
    BookModel.objects.all().delete()
    AuthorModel.objects.all().delete()
    TagModel.objects.all().delete()
    for i in range(n):
        author = AuthorModel.objects.create(name=f"Author {i}")
        book = BookModel.objects.create(title=f"Book {i}", author=author)
        t1 = TagModel.objects.create(name=f"tag-{i}-a")
        t2 = TagModel.objects.create(name=f"tag-{i}-b")
        book.tags.add(t1, t2)


_LIST_QUERY = """
query($limit: Int!) {
  books(limit: $limit) {
    title
    author { name }
    tags { name }
  }
}
"""

_BY_PK_QUERY = """
query($id: String!) {
  books_by_pk(id: $id) {
    title
    author { name }
    tags { name }
  }
}
"""


def _sql(ctx: CaptureQueriesContext) -> str:
    return "\n".join(
        f"[{i}] {q['sql']}" for i, q in enumerate(ctx.captured_queries)
    )


@pytest.mark.django_db
def test_list_query_count_is_constant_across_page_sizes() -> None:
    """N+1 would make the count GROW with the page size; the optimizer keeps
    it flat (one query for the rows + their FK JOIN, one prefetch for tags)."""
    schema = _build_schema()
    counts: dict[int, int] = {}
    last_ctx: CaptureQueriesContext | None = None
    for size in (1, 3, 10):
        _seed(size)
        with CaptureQueriesContext(connection) as ctx:
            result = schema.execute_sync(
                _LIST_QUERY, variable_values={"limit": size}
            )
        assert result.errors is None, result.errors
        assert len(result.data["books"]) == size
        assert result.data["books"][0]["author"]["name"] == "Author 0"
        assert len(result.data["books"][0]["tags"]) == 2
        counts[size] = len(ctx.captured_queries)
        last_ctx = ctx

    assert last_ctx is not None
    assert len(set(counts.values())) == 1, (
        f"query count grew with page size (N+1): {counts}\n"
        f"SQL at the largest page:\n{_sql(last_ctx)}"
    )


@pytest.mark.django_db
def test_by_pk_folds_to_one_relation_into_the_row_query() -> None:
    """by_pk composes ``optimize()`` so the to-one ``author`` is a JOIN, not a
    second SELECT: row+author = 1 query, tags prefetch = 1 query."""
    schema = _build_schema()
    _seed(3)
    target = BookModel.objects.get(title="Book 1")
    with CaptureQueriesContext(connection) as ctx:
        result = schema.execute_sync(
            _BY_PK_QUERY, variable_values={"id": str(target.pk)}
        )
    assert result.errors is None, result.errors
    assert result.data["books_by_pk"]["author"]["name"] == "Author 1"
    assert len(result.data["books_by_pk"]["tags"]) == 2
    assert len(ctx.captured_queries) == 2, (
        "by_pk did not lean on the optimizer (FK not folded into a JOIN):\n"
        f"{_sql(ctx)}"
    )
