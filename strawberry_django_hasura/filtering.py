"""Translate a Hasura ``<resource>_bool_exp`` input into a Django ``Q``.

The owner of filtering is the Django ORM; this module only maps the bounded
Hasura operator vocabulary onto ORM lookups (the map in ``CONTRACT.md``). A
Hasura ``bool_exp`` is ``{<field>: <comparison>, _and: [...], _or: [...], _not:
...}``; the python attr name of a scalar field equals its Django column (both
snake_case), so no field-name remap is needed for scalar columns.

The public ``id`` column is the one place a project may diverge: if ``id`` is
an opaque sqid (rather than the raw pk), the operand must be decoded to the pk
before the lookup. ``where_to_q`` takes an optional ``id_decode`` hook and an
``id_column`` so the sqid boundary stays the consumer's concern — this library
never inspects the value to guess whether it is a sqid (see ``AGENTS.md``).
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Mapping
from typing import Any

from django.db.models import Q
from strawberry import UNSET

# Hasura comparison attr (the python name behind the ``_eq`` wire field) ->
# (Django lookup suffix, negate?). Mirrors refine's
# hasuraFilterOperatorMappings for the operators with a portable Django ORM
# lookup. ``_nilike`` / ``_nlike`` negate their positive twin. The
# Postgres-only ``_iregex`` / ``_similar`` / ``_nsimilar`` operators (refine
# sends them for startswith/endswith CrudOps) are intentionally absent here:
# ``_iregex`` maps to Django ``__iregex`` and ``_similar`` to ``SIMILAR TO`` —
# neither is portable across backends. Add them to a project's map only on a
# backend that supports them — keep this map portable. A comparison that sets
# an operator absent from this map raises in ``comparison_to_q`` (it is never
# silently dropped — see there).
_LOOKUPS: dict[str, tuple[str, bool]] = {
    "eq": ("", False),
    "neq": ("", True),
    "gt": ("__gt", False),
    "gte": ("__gte", False),
    "lt": ("__lt", False),
    "lte": ("__lte", False),
    "in_": ("__in", False),
    "nin": ("__in", True),
    "like": ("__contains", False),
    "nlike": ("__contains", True),
    "ilike": ("__icontains", False),
    "nilike": ("__icontains", True),
    "contains": ("__contains", False),
}

_AND = "and_"
_OR = "or_"
_NOT = "not_"
_BOOL = {_AND, _OR, _NOT}


def comparison_to_q(
    field: str,
    cmp: Any,
    *,
    decode: Callable[[Any], Any] | None = None,
) -> Q:
    """AND together every operator set on one field comparison.

    ``decode`` (when given) rewrites each operand before the lookup — the sqid
    boundary for the ``id`` column. It is applied per-element for the list
    operators (``_in`` / ``_nin``).

    An operator the SDL accepts but ``_LOOKUPS`` does not map (the
    Postgres-only ``_iregex`` / ``_similar`` / ``_nsimilar`` on a backend that
    has not registered them) raises ``ValueError`` rather than being silently
    dropped: on a permission-naive read a silently-ignored filter would
    *widen* the result set. A project enables one by adding it to ``_LOOKUPS``.
    """
    q = Q()
    for attr, (suffix, negate) in _LOOKUPS.items():
        val = getattr(cmp, attr, UNSET)
        if val is UNSET:
            continue
        if decode is not None:
            val = (
                [decode(v) for v in val]
                if attr in {"in_", "nin"}
                else (decode(val))
            )
        clause = Q(**{f"{field}{suffix}": val})
        q &= ~clause if negate else clause
    is_null = getattr(cmp, "is_null", UNSET)
    if is_null is not UNSET and is_null is not None:
        clause = Q(**{f"{field}__isnull": True})
        q &= clause if is_null else ~clause
    for f in dataclasses.fields(cmp):
        if f.name in _LOOKUPS or f.name == "is_null":
            continue
        val = getattr(cmp, f.name, UNSET)
        if val is UNSET or val is None:
            continue
        raise ValueError(
            f"filter operator {f.name!r} on field {field!r} is accepted in "
            "the SDL but not mapped for this backend; register it in a "
            "project-supplied filtering._LOOKUPS or omit it"
        )
    return q


def where_to_q(
    where: Any,
    *,
    id_column: str = "pk",
    id_decode: Callable[[Any], Any] | None = None,
    field_decoders: Mapping[str, Callable[[Any], Any]] | None = None,
) -> Q:
    """Walk a Hasura ``<resource>_bool_exp`` instance into a Django ``Q``.

    ``id_column`` maps the GraphQL ``id`` field to its Django column (default
    ``pk``); ``id_decode`` decodes a sqid operand to the pk before the lookup.
    ``field_decoders`` applies the same public-id boundary to any other scalar
    field, such as a foreign-key column exposed as a public id. Both default to
    a raw-pk project; a sqid project passes its codecs.
    """
    if where is None or where is UNSET:
        return Q()
    q = Q()
    for f in dataclasses.fields(where):
        val = getattr(where, f.name, UNSET)
        if val is UNSET or val is None:
            continue
        if f.name == _AND:
            for sub in val:
                q &= where_to_q(
                    sub,
                    id_column=id_column,
                    id_decode=id_decode,
                    field_decoders=field_decoders,
                )
        elif f.name == _OR:
            any_q = Q()
            for sub in val:
                any_q |= where_to_q(
                    sub,
                    id_column=id_column,
                    id_decode=id_decode,
                    field_decoders=field_decoders,
                )
            q &= any_q
        elif f.name == _NOT:
            q &= ~where_to_q(
                val,
                id_column=id_column,
                id_decode=id_decode,
                field_decoders=field_decoders,
            )
        elif f.name == "id":
            q &= comparison_to_q(id_column, val, decode=id_decode)
        else:
            decoder = (field_decoders or {}).get(f.name)
            q &= comparison_to_q(f.name, val, decode=decoder)
    return q
