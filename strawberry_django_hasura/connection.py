"""Hasura ``limit`` / ``offset`` paging + the ``<resource>_aggregate`` shell.

Hasura lists take bare ``limit`` / ``offset`` arguments (no paging input
type) and return ``[Note!]`` directly; the row count rides the
``<resource>_aggregate { aggregate { count } }`` field, which is the SAME
container that carries the free ``<Model>Aggregate`` (see ``aggregation``).
So paging and the aggregate container live together here.

``paginate`` is the queryset-slice owner; ``make_aggregate_container``
composes ``aggregation.make_aggregate_resolver`` into the two-field
``{ aggregate, nodes }`` container the provider reads.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import strawberry
from django.db.models import QuerySet


def paginate(
    queryset: QuerySet[Any], limit: int | None, offset: int | None
) -> QuerySet[Any]:
    """Apply Hasura ``limit`` / ``offset`` to a queryset (slice).

    Offset slicing over an *unordered* queryset is non-deterministic across
    pages (the DB may return rows in a different physical order per page, so a
    paging client sees duplicated or skipped rows). When the queryset carries
    no ordering, add a ``pk`` tiebreaker to keep paging stable; a
    caller-ordered queryset (via ``order_by``) is left untouched — for paging
    over it to be deterministic that ordering must be *total* (e.g. end on a
    unique column).
    """
    if not queryset.ordered:
        queryset = queryset.order_by("pk")
    start = offset or 0
    if limit is None:
        return queryset[start:] if start else queryset
    return queryset[start : start + limit]


def make_aggregate_container(
    name: str,
    node_type: type,
    aggregate_type: type,
    *,
    filtered_queryset: Callable[[Any, Any], QuerySet[Any]],
    filtered_nodes_queryset: Callable[[Any, Any], QuerySet[Any]] | None = None,
    aggregate_resolver: Callable[[Any, Callable[[Any], QuerySet[Any]]], Any],
) -> type:
    """Build the ``<resource>_aggregate`` container type for one model.

    The container exposes Hasura's two fields over the same filtered row set:

    - ``aggregate: <Model>Aggregate`` — the native aggregate type from
    ``strawberry-django-aggregates`` (zero reshape), filled by
    ``aggregate_resolver``. - ``nodes: [<Node>!]`` — the filtered rows.

    ``name`` is the wire name (``"notes_aggregate"``). The query resolver
    constructs the container with the request's ``where``. Consumers whose
    aggregate math needs a different queryset policy than row nodes can pass
    ``filtered_nodes_queryset``; otherwise both fields derive the same
    filtered queryset.
    """

    # ``aggregate_type`` / ``node_type`` are runtime values, not names visible
    # from module global scope, so strawberry can't resolve them from a method
    # annotation. Build the resolvers as functions and set their ``return``
    # annotation explicitly (the same idiom ``AggregateBuilder`` uses for its
    # own runtime-typed fields), then attach via ``strawberry.field``.

    def resolve_aggregate(self: Any, info: strawberry.Info) -> Any:
        return aggregate_resolver(
            info, lambda i: filtered_queryset(i, self.where)
        )

    resolve_aggregate.__annotations__ = {
        "self": Any,
        "info": strawberry.Info,
        "return": aggregate_type,
    }

    def resolve_nodes(self: Any, info: strawberry.Info) -> Any:
        source = filtered_nodes_queryset or filtered_queryset
        return source(info, self.where)

    resolve_nodes.__annotations__ = {
        "self": Any,
        "info": strawberry.Info,
        "return": list[node_type],  # type: ignore[valid-type]
    }

    namespace: dict[str, Any] = {
        # Constructed by the ``<resource>_aggregate`` query resolver with the
        # caller's ``where``; field resolvers derive their filtered queryset.
        "__annotations__": {
            "where": strawberry.Private[Any],  # type: ignore[misc]
        },
        "aggregate": strawberry.field(resolver=resolve_aggregate),
        "nodes": strawberry.field(resolver=resolve_nodes),
    }
    container = type(f"{name}__container", (), namespace)
    return strawberry.type(container, name=name)
