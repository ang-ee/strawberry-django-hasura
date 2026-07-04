"""Helpers for Hasura mutation inputs (insert / update / delete by pk).

The owner of persistence is the Django model; this only translates the
Hasura input envelopes into model kwargs:

- ``insert_<resource>_one(object: <resource>_insert_input)`` →
  ``create(**...)``.
- ``update_<resource>_by_pk(pk_columns: ..., _set: <resource>_set_input)``
  → patch only the *set* fields, so an omitted column is never clobbered.
- ``delete_<resource>_by_pk(id: String)`` → the model's ``delete()``.

``input_to_dict`` is dialect-agnostic — the insert / ``_set`` envelope
reduces to the same "set (non-UNSET) fields as kwargs" as any GraphQL input.
It recurses into nested input objects (Hasura array-relationship inserts —
``<relation>: {data: [<child>...]}``) so the caller's write backend receives
plain nested dicts, never half-decoded strawberry input instances; a list of
scalar operands (an m2m ``[ID!]`` array) is left untouched because its items
are not input objects.
"""

from __future__ import annotations

import dataclasses
from typing import Any

from strawberry import UNSET


def input_to_dict(value: Any) -> dict[str, Any]:
    """Return the set (non-UNSET) fields of a strawberry input as kwargs."""
    out: dict[str, Any] = {}
    for f in dataclasses.fields(value):
        v = getattr(value, f.name, UNSET)
        if v is not UNSET:
            out[f.name] = _reduce(v)
    return out


def _reduce(value: Any) -> Any:
    """Reduce one input field value, recursing through nested input objects."""
    if _is_input_instance(value):
        return input_to_dict(value)
    if isinstance(value, (list, tuple)):
        return [_reduce(item) for item in value]
    return value


def _is_input_instance(value: Any) -> bool:
    """Return whether ``value`` is a strawberry input instance (a dataclass).

    Strawberry inputs are dataclasses, so a nested ``{data: [...]}`` envelope
    and its child rows reduce to dicts; scalars (``Decimal``, ``datetime``,
    JSON ``dict``\\ s) and public-id strings are not dataclasses and pass
    through verbatim.
    """
    return dataclasses.is_dataclass(value) and not isinstance(value, type)
