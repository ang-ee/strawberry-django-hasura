"""Hasura/NDC-shaped grouped aggregation — PREVIEW (non-stock).

``<res>_groups`` and ``<res>_groups_count`` are **not** part of the stock
``@refinedev/hasura`` contract
(that provider never sends ``group_by``). It is a forward-looking, **preview**
surface shaped to the Hasura v3 / NDC ``groups`` semantics — dimensions +
aggregates + ``having`` (over aggregates) + ``order_by`` + offset paging — for
a consumer driving the schema with a custom client.

It composes the grouping *owner*, ``strawberry-django-aggregates``, entirely
through that library's **public** surface — no fork, no private internals, no
reshape:

- the dimension spec / ``having`` / group-order INPUT types and the typed
  ``<Model>GroupKey`` all come from one ``AggregateBuilder(...).build()``
  (the SAME build that produces the free ``<Model>Aggregate``);
- the wire inputs are translated into ``compute_aggregation`` arguments by the
  builder's public ``translate_group_by`` / ``translate_having`` /
  ``translate_order_by``;
- each result row becomes ``<res>_group { key, aggregate }`` by pairing the
  builder's public ``shape_group_key`` (the typed key) with the **free**
  ``<Model>Aggregate`` via ``shape_aggregate_row``. The aggregate is *wired,
  never reshaped* (see ``CONTRACT.md`` — "the aggregate is FREE").

Enable them by building the resource with ``groupable=[...]``.
"""

from __future__ import annotations

import sys
import types
from collections.abc import Callable, Mapping
from typing import Any

import strawberry
from django.db.models import QuerySet
from django.db.models.expressions import Combinable
from strawberry_django.resolvers import django_resolver
from strawberry_django_aggregates import (
    AggregateOp,
    compute_aggregation,
    shape_aggregate_row,
)

from .aggregation import _ops_from_aggregate_blocks, _selected_fields
from .connection import capped_limit, validate_pagination
from .filtering import _filter_lookups, filter_queryset, where_to_q

GroupByExpressionProvider = Callable[
    [strawberry.Info, QuerySet[Any], list[tuple[str, Any]]],
    Mapping[str, Combinable],
]


def make_groups_field(
    *,
    builder: Any,
    built: Any,
    resource_name: str,
    filter_type: type,
    get_queryset: Any,
    id_decode: Any = None,
    id_column: str = "pk",
    field_decoders: Any = None,
    max_groups: int | None = None,
    group_key_encoders: Mapping[str, Callable[[Any], Any]] | None = None,
    filter_lookups: Mapping[str, tuple[str, bool]] | None = None,
    get_group_by_expressions: GroupByExpressionProvider | None = None,
) -> tuple[Any, Any, list[type]]:
    """Return grouped row/count fields + the generated group types.

    PREVIEW / NDC-shaped (see the module docstring). ``builder`` / ``built``
    are the model's :class:`~strawberry_django_aggregates.AggregateBuilder`
    and its ``BuiltAggregates`` — ``built.aggregate_type`` is the SAME free
    aggregate the ``<res>_aggregate`` container exposes, so the grouped
    aggregate is not a second type. Emits a ``<res>_group { key:
    <Model>GroupKey!, aggregate: <Model>Aggregate! }`` container under a
    ``<res>_groups(group_by, where, having, order_by, limit, offset)`` root
    plus its exact, unpaginated ``<res>_groups_count`` companion.
    """
    group_key_encoders = dict(group_key_encoders or {})
    filter_lookups = _filter_lookups(filter_lookups)
    module = _host_module(resource_name)
    group_key_type = built.group_key_type
    aggregate_type = built.aggregate_type
    group_by_spec = built.group_by_spec
    having_input = built.having_input
    group_order_input = built.group_order_input

    group_type = strawberry.type(
        type(
            f"{resource_name}_group",
            (),
            {
                "__module__": module.__name__,
                "__annotations__": {
                    "key": group_key_type,
                    "aggregate": aggregate_type,
                },
            },
        ),
        name=f"{resource_name}_group",
    )
    setattr(module, f"{resource_name}_group", group_type)

    def filtered_queryset(info: strawberry.Info, where: Any) -> QuerySet[Any]:
        qs: QuerySet[Any] = get_queryset(info)
        if where is not None:
            qs = filter_queryset(
                qs,
                where_to_q(
                    where,
                    id_column=id_column,
                    id_decode=id_decode,
                    field_decoders=field_decoders,
                    lookups=filter_lookups,
                ),
            )
        return qs

    def group_by_expressions(
        info: strawberry.Info,
        qs: QuerySet[Any],
        spec: list[tuple[str, Any]],
    ) -> Mapping[str, Combinable] | None:
        if get_group_by_expressions is None:
            return None
        return get_group_by_expressions(info, qs, spec)

    def resolve_groups(
        self: Any,
        info: strawberry.Info,
        group_by: list[Any],
        where: Any = None,
        having: Any = None,
        order_by: Any = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[Any]:
        del self
        validate_pagination(limit, offset)
        qs = filtered_queryset(info, where)
        # Owner-translated: wire inputs → compute_aggregation arguments. The
        # adapter never re-implements the spec / granularity / having parsing.
        spec = builder.translate_group_by(group_by)
        expressions = group_by_expressions(info, qs, spec)
        requested = _requested_group_ops(info, builder.json_paths)
        having_dict = builder.translate_having(having, requested)
        order_terms = builder.translate_order_by(order_by, spec, requested)
        rows = compute_aggregation(
            qs,
            group_by=spec,
            aggregates=requested,
            group_by_expressions=expressions,
            having=having_dict,
            order_by=order_terms,
            limit=capped_limit(limit, max_groups),
            offset=offset or 0,
            json_paths=builder.json_paths,
        )
        return [
            group_type(
                key=builder.shape_group_key(
                    group_key_type,
                    row,
                    spec,
                    value_encoders=group_key_encoders,
                ),
                aggregate=shape_aggregate_row(
                    aggregate_type,
                    row,
                    requested,
                    json_paths=builder.json_paths,
                ),
            )
            for row in rows
        ]

    resolve_groups.__annotations__ = {
        "self": Any,
        "info": strawberry.Info,
        "group_by": list[group_by_spec],  # type: ignore[valid-type]
        "where": filter_type | None,
        "having": having_input | None,
        "order_by": list[group_order_input] | None,  # type: ignore[valid-type]
        "limit": int | None,
        "offset": int | None,
        "return": list[group_type],  # type: ignore[valid-type]
    }

    def resolve_groups_count(
        self: Any,
        info: strawberry.Info,
        group_by: list[Any],
        where: Any = None,
        having: Any = None,
    ) -> int:
        del self
        qs = filtered_queryset(info, where)
        spec = builder.translate_group_by(group_by)
        expressions = group_by_expressions(info, qs, spec)
        requested = [(AggregateOp.COUNT, None)]
        having_dict = builder.translate_having(having, requested)
        return int(
            builder.count_groups(
                qs,
                spec,
                requested,
                having_dict,
                group_by_expressions=expressions,
            )
        )

    resolve_groups_count.__annotations__ = {
        "self": Any,
        "info": strawberry.Info,
        "group_by": list[group_by_spec],  # type: ignore[valid-type]
        "where": filter_type | None,
        "having": having_input | None,
        "return": int,
    }
    return (
        strawberry.field(
            resolver=django_resolver(resolve_groups),
            name=f"{resource_name}_groups",
        ),
        strawberry.field(
            resolver=django_resolver(resolve_groups_count),
            name=f"{resource_name}_groups_count",
        ),
        [
            group_type,
            group_key_type,
            group_by_spec,
            having_input,
            group_order_input,
        ],
    )


def _requested_group_ops(
    info: strawberry.Info,
    json_paths: Mapping[str, str] | None = None,
) -> list[tuple[Any, str | None]]:
    """The ``(op, field)`` pairs the client selected under ``aggregate``.

    Gathers the op blocks nested under the ``<res>_groups`` selection's
    ``aggregate`` sub-field and maps them via the shared
    :func:`~strawberry_django_hasura.aggregation._ops_from_aggregate_blocks`
    — same op vocabulary, ``count``-always, and first-seen dedupe
    (deterministic SQL) as the free ``<res>_aggregate`` resolver.
    """
    blocks = [
        agg_field
        for top in info.selected_fields
        for field in _selected_fields(top.selections)
        if field.name == "aggregate"
        for agg_field in _selected_fields(field.selections)
    ]
    return _ops_from_aggregate_blocks(blocks, json_paths)


def _host_module(name: str) -> types.ModuleType:
    """A real, importable namespace for the generated ``<res>_group`` type.

    Strawberry reads a type's ``__module__`` when registering it; giving the
    dynamically-built group type a stable synthetic module (mirroring
    ``resource._host_module``) keeps its identity stable across builds. Unlike
    the resource's inputs, the group type has no string forward refs to resolve
    — its ``key`` / ``aggregate`` annotations are already-built type objects.
    """
    module_name = f"{__name__}._generated.{name}"
    module = sys.modules.get(module_name)
    if module is None:
        module = types.ModuleType(module_name)
        sys.modules[module_name] = module
    return module
