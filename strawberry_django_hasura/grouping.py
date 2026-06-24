"""Hasura/NDC-shaped grouped aggregation — PREVIEW (non-stock).

``<res>_groups`` is **not** part of the stock ``@refinedev/hasura`` contract
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

Enable it by building the resource with ``groupable=[...]``.
"""

from __future__ import annotations

import sys
import types
from typing import Any

import strawberry
from django.db.models import QuerySet
from strawberry_django_aggregates import (
    compute_aggregation,
    shape_aggregate_row,
)

from .aggregation import _ops_from_aggregate_blocks, _selected_fields
from .filtering import where_to_q


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
) -> tuple[Any, list[type]]:
    """Return the ``<res>_groups`` field + the generated group types.

    PREVIEW / NDC-shaped (see the module docstring). ``builder`` / ``built``
    are the model's :class:`~strawberry_django_aggregates.AggregateBuilder`
    and its ``BuiltAggregates`` — ``built.aggregate_type`` is the SAME free
    aggregate the ``<res>_aggregate`` container exposes, so the grouped
    aggregate is not a second type. Emits a ``<res>_group { key:
    <Model>GroupKey!, aggregate: <Model>Aggregate! }`` container under a
    ``<res>_groups(group_by, where, having, order_by, limit, offset)`` root.
    """
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
        qs: QuerySet[Any] = get_queryset(info)
        if where is not None:
            qs = qs.filter(
                where_to_q(
                    where,
                    id_column=id_column,
                    id_decode=id_decode,
                    field_decoders=field_decoders,
                )
            )
        # Owner-translated: wire inputs → compute_aggregation arguments. The
        # adapter never re-implements the spec / granularity / having parsing.
        spec = builder.translate_group_by(group_by)
        requested = _requested_group_ops(info)
        having_dict = builder.translate_having(having, requested)
        order_terms = builder.translate_order_by(order_by, spec, requested)
        rows = compute_aggregation(
            qs,
            group_by=spec,
            aggregates=requested,
            having=having_dict,
            order_by=order_terms,
            limit=_capped(limit, max_groups),
            offset=offset or 0,
        )
        return [
            group_type(
                key=builder.shape_group_key(group_key_type, row, spec),
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
    return (
        strawberry.field(
            resolver=resolve_groups,
            name=f"{resource_name}_groups",
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
    return _ops_from_aggregate_blocks(blocks)


def _capped(limit: int | None, max_groups: int | None) -> int | None:
    """Bound an offset page by the resource's ``max_groups`` ceiling.

    NDC group pagination is offset-based; an omitted ``limit`` over a
    high-cardinality dimension would otherwise materialize every group. The
    ceiling bounds that; a smaller explicit ``limit`` is honored as-is.
    """
    if max_groups is None:
        return limit
    if limit is None:
        return max_groups
    return min(limit, max_groups)


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
