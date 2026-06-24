"""The free ``<Model>Aggregate`` — composed from the aggregates library.

This is the whole point of the Hasura dialect: the native
``<Model>Aggregate`` type that ``strawberry-django-aggregates``'
:class:`AggregateBuilder` already emits *is* Hasura's
``aggregate { count sum {…} avg {…} min {…} max {…} }``. So there is **no
reshape layer** — the nestjs path needed ~300 LOC (``build_aggregate_types``
+ ``make_aggregate_resolver`` + ``_row_to_response``) to fold flat
composite-key rows into a nested envelope; here the library's own type and
its own ``shape_aggregate_row`` do it.

This module composes three *public* primitives:

- :class:`AggregateBuilder` — owns the ``<Model>Aggregate`` TYPE
  (:func:`build_aggregate_type` returns ``builder.build().aggregate_type``).
- :func:`compute_aggregation` — runs the one aggregation query.
- :func:`shape_aggregate_row` — shapes the flat row into the type instance.

The only Hasura-specific glue is (a) walking the GraphQL selection to learn
which ``(op, field)`` pairs the client asked for — the operator vocabulary
is the Hasura wire contract this library owns — and (b) filtering via the
adapter's own ``where_to_q`` (the builder's native filter path speaks
strawberry-django filter inputs, not a hand-shaped Hasura ``bool_exp``).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

import strawberry
from django.db.models import QuerySet
from strawberry.types.nodes import SelectedField, Selection
from strawberry_django_aggregates import (
    AggregateBuilder,
    AggregateOp,
    compute_aggregation,
    shape_aggregate_row,
)

# Hasura ``aggregate`` sub-field name -> AggregateOp. The wire is snake_case
# (Hasura convention), which equals the AggregateOp *value*, so the map is
# every op keyed by its own value — no camelCase round-trip. ``count`` is
# special (no nested field; ``field=None``); the rest carry one nested type
# whose selected members name the measured columns.
_OP_FROM_WIRE: dict[str, AggregateOp] = {op.value: op for op in AggregateOp}


def build_aggregate_type(
    model: type,
    *,
    name: str | None = None,
    aggregate_fields: list[str] | None = None,
) -> type:
    """Return the native ``<Model>Aggregate`` strawberry type for a model.

    A thin pass-through to :class:`AggregateBuilder` so a consumer needs only
    this library's imports. ``aggregate_fields`` is the numeric/measurable
    column allowlist (defaults to all eligible fields); ``name`` overrides the
    type-name prefix (defaults to the model name).
    """
    built = AggregateBuilder(
        model=model, name_prefix=name, aggregate_fields=aggregate_fields
    ).build()
    # ``aggregate_type`` is ``Any`` across the untyped-import seam (the
    # aggregates library ships no ``py.typed`` yet); it is a strawberry type.
    return cast("type", built.aggregate_type)


def _selected_fields(selections: list[Selection]) -> list[SelectedField]:
    """The ``SelectedField`` leaves of a selection, flattening fragments.

    A client may request the ``aggregate`` sub-fields through a fragment spread
    or inline fragment; both carry nested ``selections`` but are not themselves
    ``SelectedField``. Recurse so requested measures are not silently dropped.
    """
    out: list[SelectedField] = []
    for selection in selections:
        if isinstance(selection, SelectedField):
            out.append(selection)
        else:
            out.extend(_selected_fields(selection.selections))
    return out


def _ops_from_aggregate_blocks(
    blocks: list[SelectedField],
) -> list[tuple[AggregateOp, str | None]]:
    """Map selected ``aggregate`` sub-fields to deduped ``(op, field)`` pairs.

    ``blocks`` are the children of an ``aggregate`` selection: ``count`` (→
    ``(COUNT, None)``) and one node per measure op (``sum``/``avg``/…), whose
    own children name the measured columns (→ ``(op, "word_count")``).
    ``count`` is always included (the non-null ``Int!``); pairs are deduped
    preserving first-seen order (deterministic SQL). Shared by the free
    ``<res>_aggregate`` resolver and the grouped ``<res>_groups`` resolver.
    """
    requested: list[tuple[AggregateOp, str | None]] = []
    for block in blocks:
        op = _OP_FROM_WIRE.get(block.name)
        if op is None:
            continue
        if op is AggregateOp.COUNT:
            requested.append((op, None))
            continue
        for member in _selected_fields(block.selections):
            requested.append((op, member.name))
    if (AggregateOp.COUNT, None) not in requested:
        requested.insert(0, (AggregateOp.COUNT, None))
    seen: set[tuple[AggregateOp, str | None]] = set()
    out: list[tuple[AggregateOp, str | None]] = []
    for entry in requested:
        if entry not in seen:
            seen.add(entry)
            out.append(entry)
    return out


def _requested_ops(
    info: strawberry.Info,
) -> list[tuple[AggregateOp, str | None]]:
    """Walk the ``aggregate { … }`` selection into ``(op, field)`` pairs.

    The resolver IS the ``aggregate`` field, so ``info.selected_fields``'
    children are the op blocks.
    """
    blocks = [
        block
        for top in info.selected_fields
        for block in _selected_fields(top.selections)
    ]
    return _ops_from_aggregate_blocks(blocks)


def make_aggregate_resolver(
    aggregate_type: type,
) -> Callable[[strawberry.Info, Callable[[Any], QuerySet[Any]]], Any]:
    """Build the ``aggregate`` resolver for a model's ``<Model>Aggregate``.

    Composes the two public aggregates primitives: derives ``(op, field)`` from
    the selection, runs one :func:`compute_aggregation`, and shapes the flat
    row with :func:`shape_aggregate_row` — zero reshape of our own. The
    returned resolver takes the per-request ``get_queryset(info)`` (the already
    row-scoped, ``where``-filtered source the ``<resource>_aggregate``
    container hands it — the same filtered queryset its ``nodes`` field uses).
    """

    def resolve(
        info: strawberry.Info, get_queryset: Callable[[Any], QuerySet[Any]]
    ) -> Any:
        requested = _requested_ops(info)
        rows = compute_aggregation(get_queryset(info), aggregates=requested)
        row = rows[0] if rows else {}
        return shape_aggregate_row(aggregate_type, row, requested)

    return resolve
