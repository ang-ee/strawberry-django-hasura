"""Apply a Hasura ``order_by: [<resource>_order_by!]`` list to a queryset.

A Hasura ``<resource>_order_by`` is a per-field input of the ``order_by`` enum
(``{word_count: desc, title: asc}``) — unlike nestjs's ``{field, direction}``
shape. A client may pass several inputs in the list; within one input several
fields may be set. Django ``.order_by()`` is the owner; this only translates
the vocabulary. The python attr name of each field equals its Django column
(both snake_case), so the clause is the field name with a ``-`` prefix for
``desc``.
"""

from __future__ import annotations

import dataclasses
import enum
from typing import Any

import strawberry
from django.core.exceptions import FieldDoesNotExist
from django.db.models import Model, QuerySet
from strawberry import UNSET


@strawberry.enum(name="order_by")
class OrderBy(enum.Enum):
    """Hasura sort direction (``order_by`` enum). Hasura also defines
    nulls-aware members (``asc_nulls_first`` …); ``asc`` / ``desc`` are the
    pair the stock ``@refinedev/hasura`` provider emits."""

    asc = "asc"
    desc = "desc"


def validate_sortable(
    model: type[Model], fields: list[str], *, id_column: str = "pk"
) -> None:
    """Allow scalar/to-one ORM paths and reject row-multiplying sorts."""
    for wire_name in fields:
        path = id_column if wire_name == "id" else wire_name
        current = model
        parts = path.split("__")
        for index, part in enumerate(parts):
            try:
                field = (
                    current._meta.pk
                    if part == "pk"
                    else current._meta.get_field(part)
                )
            except FieldDoesNotExist as exc:
                raise ValueError(
                    f"Invalid sortable field {wire_name!r}"
                ) from exc
            if field is None or field.many_to_many or field.one_to_many:
                raise ValueError(
                    f"Sortable field {wire_name!r} must not cross "
                    "a to-many relation"
                )
            if index < len(parts) - 1:
                related = field.related_model
                if not field.is_relation or related is None:
                    raise ValueError(f"Invalid sortable field {wire_name!r}")
                current = related


def order_clauses(
    order_by: list[Any] | None, *, id_column: str = "id"
) -> list[str]:
    """Flatten a Hasura ``order_by`` list into Django ``.order_by()`` clauses.

    Iterates inputs (then fields within each) in declaration order so the
    emitted clause order is deterministic and matches the wire order.
    """
    clauses: list[str] = []
    for entry in order_by or []:
        for f in dataclasses.fields(entry):
            direction = getattr(entry, f.name, UNSET)
            if direction is UNSET or direction is None:
                continue
            prefix = "-" if direction is OrderBy.desc else ""
            column = id_column if f.name == "id" else f.name
            clauses.append(f"{prefix}{column}")
    return clauses


def apply_ordering(
    queryset: QuerySet[Any],
    order_by: list[Any] | None,
    *,
    id_column: str = "id",
) -> QuerySet[Any]:
    """Apply a Hasura ``order_by`` list to a queryset (no-op when empty)."""
    clauses = order_clauses(order_by, id_column=id_column)
    return queryset.order_by(*clauses) if clauses else queryset
