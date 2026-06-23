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
from django.db.models import QuerySet
from strawberry import UNSET


@strawberry.enum(name="order_by")
class OrderBy(enum.Enum):
    """Hasura sort direction (``order_by`` enum). Hasura also defines
    nulls-aware members (``asc_nulls_first`` …); ``asc`` / ``desc`` are the
    pair the stock ``@refinedev/hasura`` provider emits."""

    asc = "asc"
    desc = "desc"


def order_clauses(order_by: list[Any] | None) -> list[str]:
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
            clauses.append(f"{prefix}{f.name}")
    return clauses


def apply_ordering(
    queryset: QuerySet[Any], order_by: list[Any] | None
) -> QuerySet[Any]:
    """Apply a Hasura ``order_by`` list to a queryset (no-op when empty)."""
    clauses = order_clauses(order_by)
    return queryset.order_by(*clauses) if clauses else queryset
