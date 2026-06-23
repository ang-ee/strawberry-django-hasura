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
            out[f.name] = v
    return out
