"""A Hasura resource whose rows come from a ``RowSource``, not a model.

The same list / aggregate(count) / by-pk SDL ``hasura_resource`` emits for a
model — ``<res>(where, order_by, limit, offset)``,
``<res>_aggregate { aggregate { count } nodes }``, ``<res>_by_pk(id)`` — but
the rows come from a caller-supplied :class:`RowSource`, not the ORM. It is
**read-only** (no insert/update/delete): computed/foreign data is served, not
written.

This is the non-model sibling of ``resource.py``. The dialect machinery is
shared via ``inputs`` (the ``<res>_bool_exp`` / ``<res>_order_by`` assembly)
and ``ordering`` (the ``order_by`` vocabulary). The one thing a model resource
gets from the Django ORM that this path must own itself is *evaluating the
dialect over Python objects*: :func:`where_matches` is the in-memory sibling of
``filtering.where_to_q`` (it interprets the same ``<res>_bool_exp`` into a
per-row predicate, not a Django ``Q``), and :func:`order_rows` /
:func:`apply_in_memory` mirror ordering + paging over a list.

``RowSource.query`` / ``RowSource.count`` are the **pushdown seam**: the
default :class:`InMemoryRowSource` evaluates everything in Python (right for
computed, already-materialised rows), while a source backed by a real transport
(a foreign daemon, a scoped queryset) implements them to push the predicate
down to its owner. Row scoping is the source's concern — this builder is
permission-naive, the same stance as ``resource.py``.
"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Callable, Iterable, Sequence
from enum import Enum
from typing import Any, Protocol

import strawberry
from strawberry import UNSET
from strawberry.types import get_object_definition
from strawberry.types.enum import StrawberryEnumDefinition

from .comparisons import IDComparison, JSONComparison
from .connection import capped_limit, validate_pagination
from .filtering import (
    PORTABLE_LOOKUPS,
    hasura_like_matches,
    validate_comparison_operand,
)
from .inputs import (
    ID_WIRE_NAME,
    build_bool_exp,
    build_order_by,
    comparison_for_python_type,
    host_module,
    pin_snake_wire_names,
)
from .ordering import order_clauses
from .resource import HasuraResource

# --- the in-memory dialect evaluator (Python sibling of where_to_q) ----------


def _ordered(
    op: Callable[[Any, Any], bool],
) -> Callable[[Any, Any], bool]:
    """Wrap an ordering predicate so it never raises mid-filter.

    A NULL row value or an incomparable value/operand pair, such as a naive
    datetime against a timezone-aware one, excludes the row.
    """

    def predicate(value: Any, operand: Any) -> bool:
        if value is None:
            return False
        try:
            return op(value, operand)
        except TypeError:
            return False

    return predicate


# Hasura comparison attr (the python name behind the ``_eq`` wire field) -> a
# ``(row_value, operand) -> bool`` predicate. Mirrors ``filtering._LOOKUPS`` —
# the same portable operator set, evaluated in Python instead of compiled to a
# Django lookup. The Postgres-only ``_iregex`` / ``_similar`` operators are
# intentionally absent (as in ``_LOOKUPS``); a comparison that sets one raises
# rather than being silently dropped (a dropped filter would widen a
# permission-naive read).
_LOOKUP_OPS: dict[str, Callable[[Any, Any], bool]] = {
    "eq": lambda value, operand: value == operand,
    "neq": lambda value, operand: value != operand,
    "gt": _ordered(lambda value, operand: value > operand),
    "gte": _ordered(lambda value, operand: value >= operand),
    "lt": _ordered(lambda value, operand: value < operand),
    "lte": _ordered(lambda value, operand: value <= operand),
    "in_": lambda value, operand: value in operand,
    "nin": lambda value, operand: value not in operand,
    # The positive ``like`` family does not match a NULL row (Django's
    # ``col LIKE x`` is unknown for NULL → excluded); the negated family does
    # match NULL (verified: Django's ``filter(~Q(col__contains=x))`` returns
    # NULL rows, the same three-valued logic as ``_neq`` / ``_nin``).
    "like": lambda value, operand: hasura_like_matches(
        value,
        operand,
        case_sensitive=True,
    ),
    "nlike": lambda value, operand: (
        not hasura_like_matches(
            value,
            operand,
            case_sensitive=True,
        )
    ),
    "ilike": lambda value, operand: hasura_like_matches(
        value,
        operand,
        case_sensitive=False,
    ),
    "nilike": lambda value, operand: (
        not hasura_like_matches(
            value,
            operand,
            case_sensitive=False,
        )
    ),
    "contains": lambda value, operand: _json_contains(value, operand),
}

# The two sibling builders must accept the same portable operator set: the
# model path compiles ``filtering._LOOKUPS`` to ORM lookups, this path runs
# ``_LOOKUP_OPS`` in Python. Adding an operator to one map but not the other
# would silently diverge the resources — fail loud at import instead.
if set(_LOOKUP_OPS) != PORTABLE_LOOKUPS:
    raise RuntimeError(
        "run_query._LOOKUP_OPS must mirror filtering._LOOKUPS key-for-key; "
        f"differs by {set(_LOOKUP_OPS) ^ PORTABLE_LOOKUPS}"
    )


def _json_equal(value: Any, operand: Any) -> bool:
    """JSON structural equality, keeping booleans distinct from numbers."""
    if isinstance(value, dict) and isinstance(operand, dict):
        return value.keys() == operand.keys() and all(
            _json_equal(value[key], item) for key, item in operand.items()
        )
    if isinstance(value, list) and isinstance(operand, list):
        return len(value) == len(operand) and all(
            _json_equal(left, right)
            for left, right in zip(value, operand, strict=True)
        )
    if type(value) in (int, float) and type(operand) in (int, float):
        return bool(value == operand)
    return type(value) is type(operand) and bool(value == operand)


def _json_contains(value: Any, operand: Any, *, nested: bool = False) -> bool:
    """JSONB containment: object subsets and unordered array subsets.

    Structure is preserved; scalar strings compare exactly. PostgreSQL's
    top-level array/primitive exception does not flatten nested containers.
    """
    if isinstance(operand, dict):
        return isinstance(value, dict) and all(
            key in value and _json_contains(value[key], item, nested=True)
            for key, item in operand.items()
        )
    if isinstance(operand, list):
        return isinstance(value, list) and all(
            any(_json_contains(item, wanted, nested=True) for item in value)
            for wanted in operand
        )
    if isinstance(value, list) and not nested:
        return any(_json_equal(item, operand) for item in value)
    return _json_equal(value, operand)


def _comparison_matches(value: Any, comparison: Any) -> bool | None:
    """AND together every operator set on one field comparison.

    The public ``id`` surface is GraphQL ``String`` (operands deserialize
    to ``str``), but a row's id may be ``int`` / ``uuid``; coerce the value
    to text for an ``IDComparison`` so ``_eq`` / ``_in`` / ``_neq`` agree with
    ``<res>_by_pk`` (which matches by string) for non-string ids.

    Unmapped operators are rejected up front by :func:`_validate_where` (once
    per request, so an empty row source still fails loud), not here.
    """
    compare_value = (
        str(value)
        if value is not None and isinstance(comparison, IDComparison)
        else value
    )
    constrained = False
    for attr, predicate in _LOOKUP_OPS.items():
        operand = getattr(comparison, attr, UNSET)
        if operand is UNSET:
            continue
        constrained = True
        if isinstance(comparison, JSONComparison) and attr in {"eq", "neq"}:
            equal = _json_equal(compare_value, operand)
            matched = equal if attr == "eq" else not equal
        else:
            matched = predicate(compare_value, operand)
        if not matched:
            return False
    is_null = getattr(comparison, "is_null", UNSET)
    if is_null is not UNSET and (value is None) != bool(is_null):
        return False
    return True if constrained or is_null is not UNSET else None


def _validate_comparison(comparison: Any) -> None:
    """Raise if a field comparison sets an operator the evaluator cannot map.

    Mirrors ``filtering.comparison_to_q``: a Postgres-only ``_iregex`` /
    ``_similar`` the SDL accepts but this path does not evaluate raises rather
    than being silently dropped — on a permission-naive read a dropped filter
    would *widen* the result set.
    """
    for field in dataclasses.fields(comparison):
        operand = getattr(comparison, field.name, UNSET)
        validate_comparison_operand(field.name, operand)
        if field.name in _LOOKUP_OPS or field.name == "is_null":
            continue
        if operand is not UNSET:
            raise ValueError(
                f"filter operator {field.name!r} is accepted in the SDL but "
                "not supported by the in-memory row source"
            )


def _validate_where(where: Any) -> None:
    """Walk a ``<res>_bool_exp`` once, raising on any unmapped operator.

    Run once per request (before iterating rows) so the fail-fast fires even
    when the row source is empty — unlike a per-row check, which a zero-row
    source would skip, silently accepting an unsupported filter.
    """
    if where is None or where is UNSET:
        return
    for field in dataclasses.fields(where):
        value = getattr(where, field.name, UNSET)
        if value is UNSET or value is None:
            continue
        if field.name in ("and_", "or_"):
            for sub in value:
                _validate_where(sub)
        elif field.name == "not_":
            _validate_where(value)
        else:
            _validate_comparison(value)


def where_matches(where: Any, row: Any) -> bool:
    """Evaluate a Hasura ``<res>_bool_exp`` instance against one row.

    The in-memory sibling of ``filtering.where_to_q``: walks the same
    ``_and`` / ``_or`` / ``_not`` + per-field comparison shape and returns a
    boolean. A field's python attr name equals its row attribute (both
    snake_case), so the value is read with ``getattr``.
    """
    _validate_where(where)
    return _where_result(where, row) is not False


def _where_result(where: Any, row: Any) -> bool | None:
    """Evaluate a validated predicate; None preserves Django's empty Q."""
    if where is None or where is UNSET:
        return None
    constrained = False
    for field in dataclasses.fields(where):
        value = getattr(where, field.name, UNSET)
        if value is UNSET or value is None:
            continue
        result: bool | None
        if field.name in {"and_", "or_"}:
            children = [
                child
                for sub in value
                if (child := _where_result(sub, row)) is not None
            ]
            combine = all if field.name == "and_" else any
            result = combine(children) if children else None
        elif field.name == "not_":
            child = _where_result(value, row)
            result = not child if child is not None else None
        else:
            result = _comparison_matches(_row_value(row, field.name), value)
        if result is False:
            return False
        constrained |= result is not None
    return True if constrained else None


def _row_value(row: Any, name: str) -> Any:
    value = getattr(row, name, None)
    # Enum comparisons use the declared string-value contract, consistently
    # for ordinary Enum, IntEnum, and string enum rows and for ordering.
    if isinstance(value, Enum):
        return str(value.value)
    # Non-pk UUID fields also use String_comparison_exp.
    return str(value) if isinstance(value, uuid.UUID) else value


def _sort_key(value: Any) -> tuple[bool, Any]:
    # NULLs sort first on ``asc``, last on ``desc`` (``order_rows`` reverses
    # the whole key) — matching the default SQLite backend the
    # project ships, so a computed resource pages NULL-bearing columns like a
    # model resource. ``value is not None`` makes the None-group ``False`` (so
    # it sorts before real values on ``asc``); the constant placeholder keeps
    # None-vs-None from raising on ``None < None`` and never cross-compares
    # against a real value (the leading flag separates the groups).
    return (value is not None, "" if value is None else value)


def _field_sorter(name: str) -> Callable[[Any], tuple[bool, Any]]:
    return lambda row: _sort_key(_row_value(row, name))


def order_rows(
    rows: Sequence[Any],
    order_by: list[Any] | None,
    *,
    id_field: str | None = None,
) -> list[Any]:
    """Apply a Hasura ``order_by`` list to rows (stable, multi-key).

    ``id_field`` (when given) is appended as the lowest-priority sort key so
    ``limit`` / ``offset`` paging is deterministic over a source whose row
    order is not stable across requests — the in-memory analogue of
    ``connection.paginate``'s ``pk`` tiebreaker. It only breaks ties the
    caller's ``order_by`` leaves, and is a no-op for rows lacking the field.
    """
    clauses = order_clauses(order_by)
    if id_field is not None:
        clauses = [*clauses, id_field]
    result = list(rows)
    for clause in reversed(clauses):
        descending = clause.startswith("-")
        field = clause[1:] if descending else clause
        result.sort(key=_field_sorter(field), reverse=descending)
    return result


def apply_in_memory(
    rows: Iterable[Any],
    where: Any,
    order_by: list[Any] | None,
    limit: int | None,
    offset: int | None,
    *,
    id_field: str | None = None,
) -> list[Any]:
    """Filter, order, and page a row iterable per the Hasura request."""
    validate_pagination(limit, offset)
    _validate_where(where)
    matched = [row for row in rows if _where_result(where, row) is not False]
    ordered = order_rows(matched, order_by, id_field=id_field)
    start = offset or 0
    return ordered[start:] if limit is None else ordered[start : start + limit]


# --- the row source seam (pushdown) ------------------------------------------


class RowSource(Protocol):
    """The caller-supplied seam that satisfies one Hasura request over rows.

    ``query`` returns the filtered + ordered + paged page; ``count`` returns
    the filtered (unpaged) total for ``<res>_aggregate.aggregate.count``. Both
    receive the parsed ``where`` so a transport-backed source can push the
    predicate down (e.g. a foreign daemon, a scoped queryset); the default
    :class:`InMemoryRowSource` evaluates it in Python.
    """

    def query(
        self,
        info: strawberry.Info,
        *,
        where: Any,
        order_by: list[Any] | None,
        limit: int | None,
        offset: int | None,
    ) -> list[Any]: ...

    def count(self, info: strawberry.Info, *, where: Any) -> int: ...


class InMemoryRowSource:
    """A :class:`RowSource` over rows materialised in Python per request.

    ``get_rows(info)`` returns the full row iterable (e.g. computed schema
    introspection); the source then filters / orders / pages / counts it with
    the in-memory dialect evaluator. Right for already-materialised, bounded
    data — there is no transport to push the predicate down to.

    Each query/count call obtains freshly scoped rows. Context objects can
    outlive a GraphQL operation, so this source never caches rows on context.
    A consumer may memoise inside an explicitly operation-scoped get_rows.
    """

    def __init__(
        self,
        get_rows: Callable[[strawberry.Info], Iterable[Any]],
        *,
        id_field: str = ID_WIRE_NAME,
    ):
        self._get_rows = get_rows
        # The stable paging tiebreaker (mirrors the queryset path's ``pk``).
        # Match the resource's ``id_field`` when it is not the default ``id``.
        self._id_field = id_field

    def _rows(self, info: strawberry.Info) -> list[Any]:
        return list(self._get_rows(info))

    def query(
        self,
        info: strawberry.Info,
        *,
        where: Any,
        order_by: list[Any] | None,
        limit: int | None,
        offset: int | None,
    ) -> list[Any]:
        validate_pagination(limit, offset)
        return apply_in_memory(
            self._rows(info),
            where,
            order_by,
            limit,
            offset,
            id_field=self._id_field,
        )

    def count(self, info: strawberry.Info, *, where: Any) -> int:
        _validate_where(where)
        return sum(
            1
            for row in self._rows(info)
            if _where_result(where, row) is not False
        )


# --- the builder -------------------------------------------------------------


def _node_field_python_types(node: type) -> dict[str, Any]:
    """Map each node field's python attr name to the scalar it carries.

    Keyed by ``python_name`` — the attribute ``where_matches`` / ``order_rows``
    read off a row with ``getattr``, and the name a ``filterable`` /
    ``sortable`` column refers to — not the wire name. A node field with an
    explicit camelCase ``strawberry.field(name=...)`` would otherwise be
    filtered/sorted against the (absent) wire attribute, matching nothing.
    """
    definition = get_object_definition(node)
    if definition is None:
        raise TypeError(f"{node!r} is not a strawberry type")
    return {
        field.python_name: _python_type_of(field.type)
        for field in definition.fields
    }


def _python_type_of(field_type: Any) -> Any:
    """The python scalar a strawberry field type carries (for comparison).

    Unwraps Optional/List, then defers the scalar -> comparison mapping to the
    shared ``inputs.comparison_for_python_type`` owner (which maps
    str/int/float/Decimal/bool/datetime/date/uuid/JSON and raises on the
    genuinely unmappable — the library's fail-fast stance). The one exception
    the model path does not need: a GraphQL enum filters as its string value.
    """
    while hasattr(field_type, "of_type"):
        field_type = field_type.of_type
    if isinstance(field_type, StrawberryEnumDefinition) or hasattr(
        field_type, "_enum_definition"
    ):
        return str
    if (
        field_type is strawberry.ID
        or getattr(field_type, "__name__", None) == "ID"
    ):
        return strawberry.ID
    return field_type


def _count_aggregate_type(name: str) -> type:
    """Build the minimal ``<Resource>Aggregate { count: Int! }`` row payload.

    Unlike the model path's free ``<Model>Aggregate`` (the SQL aggregate
    compiler), a computed resource only needs the row total for pagination, so
    its aggregate is count-only.
    """
    aggregate = type(
        f"{name}__aggregate", (), {"__annotations__": {"count": int}}
    )
    return strawberry.type(aggregate, name=f"{name}Aggregate")


def _aggregate_container(
    res: str,
    node: type,
    source: RowSource,
    count_type: type,
    max_rows: int | None,
) -> type:
    """Build the ``<res>_aggregate { aggregate, nodes }`` container.

    Deliberately mirrors ``connection.make_aggregate_container`` for the
    count-only / non-queryset path: that owner's ``filtered_queryset`` /
    ``aggregate_resolver`` seam is queryset-shaped, so here the source's
    ``count`` / ``query`` fill the same ``{ aggregate, nodes }`` shell.
    """

    def resolve_aggregate(self: Any, info: strawberry.Info) -> Any:
        return count_type(count=source.count(info, where=self.where))

    resolve_aggregate.__annotations__ = {
        "self": Any,
        "info": strawberry.Info,
        "return": count_type,
    }

    def resolve_nodes(self: Any, info: strawberry.Info) -> Any:
        return source.query(
            info, where=self.where, order_by=None, limit=max_rows, offset=None
        )

    resolve_nodes.__annotations__ = {
        "self": Any,
        "info": strawberry.Info,
        "return": list[node],  # type: ignore[valid-type]
    }

    namespace: dict[str, Any] = {
        "__annotations__": {"where": strawberry.Private[Any]},  # type: ignore[misc]
        "aggregate": strawberry.field(resolver=resolve_aggregate),
        "nodes": strawberry.field(resolver=resolve_nodes),
    }
    container = type(f"{res}__container", (), namespace)
    return strawberry.type(container, name=f"{res}_aggregate")


def hasura_run_query_resource(
    node: type,
    *,
    name: str,
    filterable: Sequence[str],
    sortable: Sequence[str],
    source: RowSource,
    id_field: str = ID_WIRE_NAME,
    max_rows: int | None = None,
    aggregate_name: str | None = None,
) -> HasuraResource:
    """Assemble a read-only Hasura resource over a :class:`RowSource`.

    ``node`` is the strawberry row type (the comparison/order scalar of each
    column is read from its field types); ``name`` is the resource stem (the
    plural Hasura name). ``filterable`` / ``sortable`` are the
    ``<res>_bool_exp`` / ``<res>_order_by`` column allowlists. ``source``
    reads, filters, orders, pages and counts the rows (the pushdown seam).
    ``id_field`` is the node field ``<res>_by_pk`` matches (its comparison is
    the String-typed ``ID`` surface). ``max_rows`` optionally caps list and
    aggregate nodes responses, including requests that omit ``limit``;
    aggregate counts remain exact and unpaginated. ``aggregate_name``
    overrides the resource-based aggregate type prefix; use it to retain
    a previously published node-based type name.

    Returns a :class:`HasuraResource` with an empty mutation holder (read-only)
    whose ``query`` / ``types`` drop into a schema alongside model resources.
    """
    res = name
    capped_limit(None, max_rows)
    module = host_module(res)
    field_types = _node_field_python_types(node)
    missing = [
        col for col in (*filterable, *sortable) if col not in field_types
    ]
    if missing:
        raise TypeError(
            f"hasura_run_query_resource({name!r}) declares unknown node "
            f"field(s) {missing!r}"
        )
    if id_field not in filterable:
        # ``by_pk`` resolves through an ``id _eq`` ``<res>_bool_exp`` so a
        # transport source pushes the lookup down; that requires ``id_field``
        # to be a generated comparison column. (The Hasura dialect filters by
        # id anyway — ``<res>_bool_exp.id`` always exists.)
        raise TypeError(
            f"hasura_run_query_resource({name!r}) id_field {id_field!r} must "
            f"be listed in filterable {list(filterable)!r}"
        )
    bool_exp = build_bool_exp(
        res,
        {
            col: comparison_for_python_type(
                field_types[col], public_id=col == id_field
            )
            for col in filterable
        },
        module,
    )
    order_by_input = build_order_by(res, list(sortable), module)
    count_type = _count_aggregate_type(aggregate_name or res)
    container = _aggregate_container(res, node, source, count_type, max_rows)

    def resolve_list(
        self: Any,
        info: strawberry.Info,
        where: Any = None,
        order_by: Any = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[Any]:
        validate_pagination(limit, offset)
        return source.query(
            info,
            where=where,
            order_by=order_by,
            limit=capped_limit(limit, max_rows),
            offset=offset,
        )

    resolve_list.__annotations__ = {
        "self": Any,
        "info": strawberry.Info,
        "where": bool_exp | None,
        "order_by": list[order_by_input] | None,  # type: ignore[valid-type]
        "limit": int | None,
        "offset": int | None,
        "return": list[node],  # type: ignore[valid-type]
    }

    def resolve_aggregate(
        self: Any, info: strawberry.Info, where: Any = None
    ) -> Any:
        return container(where=where)

    resolve_aggregate.__annotations__ = {
        "self": Any,
        "info": strawberry.Info,
        "where": bool_exp | None,
        "return": container,
    }

    def resolve_by_pk(self: Any, info: strawberry.Info, id: str) -> Any | None:
        # Push an ``id _eq`` predicate through the source (a transport source
        # turns it into an indexed lookup; the in-memory source filters it) and
        # bound to one row — instead of pulling the whole dataset and scanning.
        # Routing through the same ``<res>_bool_exp`` keeps ``by_pk`` and the
        # list's ``id { _eq }`` filter byte-for-byte consistent.
        where = bool_exp(**{id_field: IDComparison(eq=id)})
        rows = source.query(
            info, where=where, order_by=None, limit=1, offset=None
        )
        return rows[0] if rows else None

    resolve_by_pk.__annotations__ = {
        "self": Any,
        "info": strawberry.Info,
        "id": str,
        "return": node | None,
    }

    list_root = res
    aggregate_root = f"{res}_aggregate"
    detail_root = f"{res}_by_pk"
    query_fields = {
        list_root: strawberry.field(resolver=resolve_list, name=list_root),
        aggregate_root: strawberry.field(
            resolver=resolve_aggregate, name=aggregate_root
        ),
        detail_root: strawberry.field(
            resolver=resolve_by_pk, name=detail_root
        ),
    }
    query = strawberry.type(type(f"{res}__query", (), query_fields))
    pin_snake_wire_names(query)
    # Read-only: an empty mutation holder keeps the bundle shape uniform with
    # the model path's all-ops-disabled resource (it merges to nothing; an
    # addon serving the resource read-only simply does not register it).
    mutation = strawberry.type(type(f"{res}__mutation", (), {}))
    return HasuraResource(
        query=query,
        mutation=mutation,
        types=[container, count_type, bool_exp, order_by_input],
        name=res,
        node_type=node,
        filter_type=bool_exp,
        order_by_type=order_by_input,
        aggregate_container_type=container,
        aggregate_type=count_type,
        list_root=list_root,
        aggregate_root=aggregate_root,
        detail_root=detail_root,
    )
