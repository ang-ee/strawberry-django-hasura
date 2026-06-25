"""Generated Hasura input types — the model-independent assembly.

``<res>_bool_exp`` and ``<res>_order_by`` are built from a field → comparison
mapping; *how* that mapping is derived — from a **Django field** (the model
builder in ``resource.py``) or a **declared python scalar** (the run_query
builder in ``run_query.py``) — is the caller's concern. These primitives own
only the strawberry input construction, the comparison-scalar vocabulary, and
the snake_case wire pinning that both builders share.

The generated ``<res>_bool_exp`` references itself (``_and`` / ``_or`` /
``_not``); strawberry resolves those forward refs against the type's module
globals at schema build, so the generated types are hosted in a per-resource
synthetic module.
"""

from __future__ import annotations

import datetime
import decimal
import sys
import types
import uuid
from typing import Any, cast

import strawberry
from strawberry import UNSET
from strawberry.scalars import JSON
from strawberry.types import get_object_definition

from .comparisons import (
    BooleanComparison,
    DateTimeComparison,
    FloatComparison,
    IDComparison,
    IntComparison,
    JSONComparison,
    StringComparison,
)
from .ordering import OrderBy

#: The fixed refine ``idType`` wire field name: its comparison is always
#: ``IDComparison`` (the String-typed pk surface) — see ``CONTRACT.md``.
ID_WIRE_NAME = "id"

#: The fact this module owns: the Hasura ``*_comparison_exp`` input for each
#: python scalar (its filter operator vocabulary). ``strawberry.ID`` is the
#: pk / public ``id`` surface (String-typed args refine's ``idType`` binds).
COMPARISON_FOR_TYPE: dict[Any, type] = {
    str: StringComparison,
    uuid.UUID: StringComparison,
    int: IntComparison,
    float: FloatComparison,
    decimal.Decimal: FloatComparison,
    bool: BooleanComparison,
    datetime.datetime: DateTimeComparison,
    datetime.date: DateTimeComparison,
    datetime.time: DateTimeComparison,
    strawberry.ID: IDComparison,
    JSON: JSONComparison,
}


def comparison_for_python_type(
    python: Any, *, public_id: bool = False
) -> type:
    """Return the ``*_comparison_exp`` input for a python scalar.

    ``public_id`` forces the ``ID`` comparison (the String-typed pk surface).
    A python type with no mapped comparison raises rather than silently
    degrading — the library's fail-fast stance.
    """
    if public_id:
        return IDComparison
    comparison = COMPARISON_FOR_TYPE.get(python)
    if comparison is None:
        raise TypeError(
            f"python type {python!r} has no Hasura comparison input; "
            "it is not a filterable scalar"
        )
    return comparison


def pin_snake_wire_names(
    strawberry_type: type,
    seen: set[int] | None = None,
) -> None:
    """Pin each field's GraphQL wire name to its snake_case python name.

    Walks the type (and its nested object types) and, where strawberry would
    otherwise camelCase a snake_case python identifier, pins the verbatim snake
    name. An explicit ``strawberry.field(name=...)`` already set a
    ``graphql_name`` and is left untouched. Same wire effect as
    :func:`~strawberry_django_hasura.naming.hasura_config`, scoped to the
    generated types, so a consumer on a camelCase schema gets snake_case wire
    names for the resource without a schema-wide converter.
    """
    definition = get_object_definition(strawberry_type)
    if definition is None:
        return
    seen = seen or set()
    marker = id(definition)
    if marker in seen:
        return
    seen.add(marker)
    for field in definition.fields:
        if field.graphql_name is None and "_" in field.python_name:
            field.graphql_name = field.python_name
        for arg in field.arguments:
            if arg.graphql_name is None and "_" in arg.python_name:
                arg.graphql_name = arg.python_name
        inner = field.type
        while hasattr(inner, "of_type"):
            inner = inner.of_type
        nested = get_object_definition(inner)
        if nested is not None and nested is not definition:
            pin_snake_wire_names(cast("type", inner), seen)


def host_module(name: str) -> types.ModuleType:
    """A synthetic module to host a resource's generated types.

    The ``<res>_bool_exp`` input references itself (``_and`` / ``_or`` /
    ``_not``); strawberry resolves those forward references against the type's
    module globals at schema-build time, so the generated types must live in a
    real, importable module namespace.
    """
    module_name = f"{__name__}._generated.{name}"
    module = sys.modules.get(module_name)
    if module is None:
        module = types.ModuleType(module_name)
        sys.modules[module_name] = module
    return module


def input_type(
    name: str,
    annotations: dict[str, Any],
    *,
    module: types.ModuleType,
    defaults: dict[str, Any] | None = None,
    fields: dict[str, Any] | None = None,
) -> type:
    """Build a ``strawberry.input`` from a name → annotation mapping.

    ``defaults`` supplies a per-attr default value (a required field is left
    out) and ``fields`` a per-attr ``strawberry.field(...)`` (the ``_and`` /
    ``_or`` / ``_not`` wire names). Hosted in ``module`` so forward refs
    resolve; the snake_case column names it declares are pinned verbatim.
    """
    namespace: dict[str, Any] = {
        "__annotations__": annotations,
        "__module__": module.__name__,
    }
    for attr, value in (defaults or {}).items():
        namespace[attr] = value
    for attr, value in (fields or {}).items():
        namespace[attr] = value
    built = strawberry.input(type(name, (), namespace), name=name)
    setattr(module, name, built)
    pin_snake_wire_names(built)
    return built


def build_bool_exp(
    res: str,
    comparisons_by_field: dict[str, type],
    module: types.ModuleType,
) -> type:
    """Build the self-referential ``<res>_bool_exp`` from field comparisons.

    ``comparisons_by_field`` maps each filterable field to its
    ``*_comparison_exp`` input class; the caller resolves those (from a Django
    field or a declared scalar). Adds the boolean composition
    (``_and`` / ``_or`` / ``_not``).
    """
    bool_exp_name = f"{res}_bool_exp"
    annotations: dict[str, Any] = {
        field: comparison | None
        for field, comparison in comparisons_by_field.items()
    }
    defaults: dict[str, Any] = dict.fromkeys(comparisons_by_field, UNSET)
    annotations |= {
        "and_": f"list[{bool_exp_name}] | None",
        "or_": f"list[{bool_exp_name}] | None",
        "not_": f"{bool_exp_name} | None",
    }
    fields = {
        "and_": strawberry.field(name="_and", default=UNSET),
        "or_": strawberry.field(name="_or", default=UNSET),
        "not_": strawberry.field(name="_not", default=UNSET),
    }
    return input_type(
        bool_exp_name,
        annotations,
        module=module,
        defaults=defaults,
        fields=fields,
    )


def build_order_by(
    res: str,
    sortable: list[str],
    module: types.ModuleType,
) -> type:
    """Build the ``<res>_order_by`` input (per-field ``order_by`` enum)."""
    return input_type(
        f"{res}_order_by",
        dict.fromkeys(sortable, OrderBy | None),
        module=module,
        defaults=dict.fromkeys(sortable, UNSET),
    )
