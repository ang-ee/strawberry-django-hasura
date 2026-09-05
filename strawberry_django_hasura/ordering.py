"""Apply a Hasura ``order_by: [<resource>_order_by!]`` list to a queryset.

A Hasura ``<resource>_order_by`` is a per-field input of the ``order_by`` enum
(``{word_count: desc, title: asc}``) — unlike nestjs's ``{field, direction}``
shape. A client may pass several inputs in the list; within one input several
fields may be set. Django ``.order_by()`` is the owner; this only translates
the vocabulary. Explicit sortable aliases map wire names to existing queryset
annotations;
other fields retain their Django column/path names. ``desc`` adds a ``-``
prefix, and the primary key makes explicit ordering total.
"""

from __future__ import annotations

import dataclasses
import enum
import re
from collections.abc import Mapping
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
    model: type[Model],
    fields: list[str],
    *,
    id_column: str = "pk",
    sortable_aliases: Mapping[str, str] | None = None,
) -> None:
    """Allow scalar/to-one ORM paths and reject row-multiplying sorts."""
    aliases = sortable_aliases or {}
    native_names = {
        name
        for field in model._meta.get_fields()
        for name in (field.name, getattr(field, "attname", field.name))
    } | {"id", "pk", id_column}
    for wire_name, annotation in aliases.items():
        if (
            not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", wire_name)
            or "__" in wire_name
            or wire_name in native_names
            or wire_name not in fields
        ):
            raise ValueError(
                f"Sortable alias {wire_name!r} must be a declared, "
                "non-colliding wire field"
            )
        if (
            not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", annotation)
            or "__" in annotation
            or annotation in native_names
        ):
            raise ValueError(
                f"Sortable alias {wire_name!r} must target an annotation "
                "identifier, not a model field or path"
            )
    for wire_name in fields:
        if wire_name in aliases:
            continue
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
    order_by: list[Any] | None,
    *,
    id_column: str = "id",
    sortable_aliases: Mapping[str, str] | None = None,
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
            column = (sortable_aliases or {}).get(f.name, f.name)
            if f.name == "id":
                column = id_column
            clauses.append(f"{prefix}{column}")
    return clauses


def apply_ordering(
    queryset: QuerySet[Any],
    order_by: list[Any] | None,
    *,
    id_column: str = "id",
    sortable_aliases: Mapping[str, str] | None = None,
) -> QuerySet[Any]:
    """Order native fields/annotations, adding a PK tie breaker when absent.

    The source owns annotation expressions and row cardinality. Only selected
    aliases must be present; an empty input preserves source ordering.
    """
    clauses = order_clauses(
        order_by, id_column=id_column, sortable_aliases=sortable_aliases
    )
    if not clauses:
        return queryset
    selected = {clause.removeprefix("-") for clause in clauses}
    for wire_name, annotation in (sortable_aliases or {}).items():
        if (
            annotation in selected
            and annotation not in queryset.query.annotations
        ):
            raise ValueError(
                f"Sortable alias {wire_name!r} requires queryset annotation "
                f"{annotation!r}"
            )
    pk = queryset.model._meta.pk
    if pk is not None and not selected.intersection(
        {"pk", pk.name, pk.attname}
    ):
        clauses.append("pk")
    return queryset.order_by(*clauses)
