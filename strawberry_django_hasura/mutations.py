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
plain nested dicts, never half-decoded strawberry input instances. Only a
declared strawberry **input** reduces: any other value — a scalar, a list of
scalar operands (an m2m ``[ID!]`` array), a tuple, or a custom-scalar value
that happens to be a dataclass — passes through verbatim.
"""

from __future__ import annotations

from typing import Any

from strawberry import UNSET
from strawberry.types import get_object_definition


def input_to_dict(value: Any) -> dict[str, Any]:
    """Return declared fields and public instance extras, skipping UNSET."""
    out: dict[str, Any] = {}
    fields = value.__strawberry_definition__.fields
    declared_names = {field.python_name for field in fields}
    for field in fields:
        v = getattr(value, field.python_name, UNSET)
        if v is not UNSET:
            out[field.python_name] = _reduce(v)
    # Input extensions may exist only as values on the converted instance.
    # Slotted inputs without an instance dictionary have no extras.
    for name, v in getattr(value, "__dict__", {}).items():
        if (
            name not in declared_names
            and not name.startswith("_")
            and v is not UNSET
        ):
            out[name] = _reduce(v)
    return out


def _reduce(value: Any) -> Any:
    """Reduce one input field value, recursing through nested input objects."""
    if _is_input_instance(value):
        return input_to_dict(value)
    if isinstance(value, list):
        return [_reduce(item) for item in value]
    return value


def _is_input_instance(value: Any) -> bool:
    """Return whether ``value`` is a strawberry **input** instance.

    The check is strawberry's own type definition (``is_input``), not "is a
    dataclass": a custom scalar may parse to a plain dataclass value (a
    ``Money``/``GeoPoint`` object) that must reach the write backend intact,
    never flattened to a dict.
    """
    definition = get_object_definition(type(value))
    return definition is not None and definition.is_input
