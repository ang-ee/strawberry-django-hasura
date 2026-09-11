"""Apply a Hasura ``order_by: [<resource>_order_by!]`` list to a queryset.

A Hasura ``<resource>_order_by`` is a per-field input of the ``order_by`` enum
(``{word_count: desc, title: asc}``) — unlike nestjs's ``{field, direction}``
shape. A client may pass several inputs in the list; within one input several
fields may be set. Django ``.order_by()`` is the owner; this only translates
the vocabulary. Explicit sortable aliases map wire names to queryset
annotations — either installed by the source (a plain string) or prepared
lazily by :func:`prepare_sort_aliases` from a :class:`SortAlias` expression
provider, only when the alias is ordered. Other fields retain their Django
column/path names. ``desc`` adds a ``-`` prefix, and the primary key makes
explicit ordering total.
"""

from __future__ import annotations

import dataclasses
import enum
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import strawberry
from django.core.exceptions import FieldDoesNotExist
from django.db.models import Model, QuerySet
from django.db.models.expressions import Combinable
from strawberry import UNSET

SortAliasExpression = Callable[[strawberry.Info, QuerySet[Any]], Combinable]
"""Lazy alias provider: ``(info, queryset) -> <Django expression>``.

``queryset`` is the request's already-scoped and ``where``-filtered source;
return a per-row expression (``F``, ``Func``, an ``OuterRef``-correlated
``Subquery``, …) for ``.alias()``. Deriving data from ``queryset`` itself
makes the sort key depend on the request's ``where``.
"""


@strawberry.enum(name="order_by")
class OrderBy(enum.Enum):
    """Hasura sort direction (``order_by`` enum). Hasura also defines
    nulls-aware members (``asc_nulls_first`` …); ``asc`` / ``desc`` are the
    pair the stock ``@refinedev/hasura`` provider emits."""

    asc = "asc"
    desc = "desc"


@dataclass(frozen=True)
class SortAlias:
    """A sortable wire alias targeting queryset annotation ``path``.

    ``SortAlias("_x")`` (or the plain string ``"_x"``) expects the source to
    have installed the annotation. With an ``expression`` provider the
    annotation is prepared lazily — :func:`prepare_sort_aliases` calls
    ``expression(info, queryset)`` only when ``order_by`` selects the alias
    and installs the result via ``.alias()`` without selecting it.
    """

    path: str
    expression: SortAliasExpression | None = None

    def prepare(
        self, info: strawberry.Info, queryset: QuerySet[Any]
    ) -> QuerySet[Any]:
        """Install the alias without selecting its value."""

        if self.expression is None:
            return queryset
        if self.path in queryset.query.annotations:
            raise ValueError(
                f"Sortable alias annotation {self.path!r} is already "
                "installed by the source; declare it as a plain alias"
            )
        value = self.expression(info, queryset)
        if not isinstance(value, Combinable):
            raise ValueError(
                f"Sortable alias expression for {self.path!r} must return "
                f"a Django expression, not {type(value).__name__}"
            )
        prepared: QuerySet[Any] = queryset.alias(**{self.path: value})
        return prepared


def _sort_aliases(
    aliases: Mapping[str, str | SortAlias] | None,
) -> dict[str, SortAlias]:
    """Normalize the public alias declaration at the ordering boundary."""

    normalized: dict[str, SortAlias] = {}
    for name, alias in (aliases or {}).items():
        if isinstance(alias, str):
            alias = SortAlias(alias)
        elif not isinstance(alias, SortAlias):
            raise ValueError(
                f"Sortable alias {name!r} must map to an annotation name "
                "or a SortAlias"
            )
        normalized[name] = alias
    return normalized


def validate_sortable(
    model: type[Model],
    fields: list[str],
    *,
    id_column: str = "pk",
    sortable_aliases: Mapping[str, str | SortAlias] | None = None,
) -> dict[str, SortAlias]:
    """Allow scalar/to-one ORM paths and reject row-multiplying sorts.

    Returns the normalized alias mapping so a builder validates and
    normalizes once.
    """
    aliases = _sort_aliases(sortable_aliases)
    lazy_paths: dict[str, str] = {}
    native_names = {
        name
        for field in model._meta.get_fields()
        for name in (field.name, getattr(field, "attname", field.name))
    } | {"id", "pk", id_column}
    for wire_name, alias in aliases.items():
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
            not isinstance(alias.path, str)
            or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", alias.path)
            or "__" in alias.path
            or alias.path in native_names
        ):
            raise ValueError(
                f"Sortable alias {wire_name!r} must target an annotation "
                "identifier, not a model field or path"
            )
        if alias.expression is not None and not callable(alias.expression):
            raise ValueError(
                f"Sortable alias {wire_name!r} expression must be a "
                "callable provider"
            )
        if alias.expression is not None:
            lazy_paths[alias.path] = wire_name
    for wire_name, alias in aliases.items():
        owner = lazy_paths.get(alias.path)
        if owner is not None and owner != wire_name:
            raise ValueError(
                f"Sortable alias {wire_name!r} shares lazily prepared "
                f"annotation {alias.path!r} with {owner!r}"
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
    return aliases


def _selected_columns(clauses: list[str]) -> set[str]:
    return {clause.removeprefix("-") for clause in clauses}


def prepare_sort_aliases(
    queryset: QuerySet[Any],
    order_by: list[Any] | None,
    *,
    info: strawberry.Info,
    id_column: str = "id",
    sortable_aliases: Mapping[str, str | SortAlias] | None = None,
) -> QuerySet[Any]:
    """Install lazily declared alias annotations selected by ``order_by``.

    Compose this before :func:`apply_ordering`; it runs each selected
    :class:`SortAlias` expression provider once and leaves the queryset
    untouched when nothing lazy is ordered. Name translation itself stays in
    :func:`apply_ordering`.
    """
    aliases = _sort_aliases(sortable_aliases)
    clauses = _order_clauses(order_by, id_column=id_column, aliases=aliases)
    if not clauses:
        return queryset
    selected = _selected_columns(clauses)
    for alias in aliases.values():
        if alias.expression is not None and alias.path in selected:
            queryset = alias.prepare(info, queryset)
    return queryset


def order_clauses(
    order_by: list[Any] | None,
    *,
    id_column: str = "id",
    sortable_aliases: Mapping[str, str | SortAlias] | None = None,
) -> list[str]:
    """Flatten a Hasura ``order_by`` list into Django ``.order_by()`` clauses.

    Iterates inputs (then fields within each) in declaration order so the
    emitted clause order is deterministic and matches the wire order.
    """
    aliases = _sort_aliases(sortable_aliases)
    return _order_clauses(order_by, id_column=id_column, aliases=aliases)


def _order_clauses(
    order_by: list[Any] | None,
    *,
    id_column: str,
    aliases: Mapping[str, SortAlias],
) -> list[str]:
    """Build clauses from aliases already normalized at the public boundary."""

    clauses: list[str] = []
    for entry in order_by or []:
        for f in dataclasses.fields(entry):
            direction = getattr(entry, f.name, UNSET)
            if direction is UNSET or direction is None:
                continue
            prefix = "-" if direction is OrderBy.desc else ""
            column = aliases[f.name].path if f.name in aliases else f.name
            if f.name == "id":
                column = id_column
            clauses.append(f"{prefix}{column}")
    return clauses


def apply_ordering(
    queryset: QuerySet[Any],
    order_by: list[Any] | None,
    *,
    id_column: str = "id",
    sortable_aliases: Mapping[str, str | SortAlias] | None = None,
) -> QuerySet[Any]:
    """Order native fields/annotations, adding a PK tie breaker when absent.

    This only translates names and never adds SQL: the source (or a prior
    :func:`prepare_sort_aliases`) owns annotation expressions and row
    cardinality. Only selected aliases must be present; an empty input
    preserves source ordering.
    """
    aliases = _sort_aliases(sortable_aliases)
    clauses = _order_clauses(order_by, id_column=id_column, aliases=aliases)
    if not clauses:
        return queryset
    selected = _selected_columns(clauses)
    for wire_name, alias in aliases.items():
        annotation = alias.path
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
