"""Hasura/NDC-shaped grouped aggregation — PREVIEW (non-stock).

``<res>_groups`` and ``<res>_groups_count`` are **not** part of the stock
``@refinedev/hasura`` contract
(that provider never sends ``group_by``). It is a forward-looking, **preview**
surface shaped to the Hasura v3 / NDC ``groups`` semantics — dimensions +
aggregates + ``having`` (over aggregates) + ``order_by`` + offset paging — for
a consumer driving the schema with a custom client.

It composes the grouping *owner*, ``strawberry-django-aggregates``, entirely
through that library's **public** surface — no fork, no private internals, no
reshape:

- the dimension spec / ``having`` / group-order INPUT types and the typed
  ``<Model>GroupKey`` all come from one ``AggregateBuilder(...).build()``
  (the SAME build that produces the free ``<Model>Aggregate``);
- the wire inputs are translated into ``compute_aggregation`` arguments by the
  builder's public ``translate_group_by`` / ``translate_having`` /
  ``translate_order_by``;
- each result row becomes ``<res>_group { key, aggregate }`` by pairing the
  builder's public ``shape_group_key`` (the typed key) with the **free**
  ``<Model>Aggregate`` via ``shape_aggregate_row``. The aggregate is *wired,
  never reshaped* (see ``CONTRACT.md`` — "the aggregate is FREE").

Enable them by building the resource with ``groupable=[...]``.

The field assembly is shared by both resource builders. Only execution
differs: a model resource groups its filtered queryset in SQL
(``compute_aggregation`` / ``count_groups``); a ``RowSource`` resource groups
the rows its source returns for the same ``where`` in memory
(``compute_row_aggregation`` over the source's row model). Translation, key and
aggregate shaping, paging caps and encoder validation have one implementation.

A caller may declare extra ``<res>_group`` fields (``group_fields``): each
is filled for a whole page of groups by one batch callback, so a consumer can
carry per-group data (for example each group's first rows) in the same
response without one read per group. Without them the emitted schema is
unchanged.
"""

from __future__ import annotations

import dataclasses
import sys
import types
from collections.abc import Callable, Mapping, Sequence
from inspect import Parameter, Signature
from keyword import iskeyword
from typing import Any, Protocol

import strawberry
from django.db.models import Q, QuerySet
from django.db.models.expressions import Combinable
from strawberry_django.resolvers import django_resolver
from strawberry_django_aggregates import (
    AggregateOp,
    compute_aggregation,
    compute_row_aggregation,
    shape_aggregate_row,
)

from .aggregation import _ops_from_aggregate_blocks, _selected_fields
from .connection import capped_limit, validate_pagination
from .filtering import _filter_lookups, filter_queryset, where_to_q
from .inputs import caller_parameters, pin_snake_wire_names

GroupByExpressionProvider = Callable[
    [strawberry.Info, QuerySet[Any], list[tuple[str, Any]]],
    Mapping[str, Combinable],
]

Spec = list[tuple[str, Any]]
Requested = list[tuple[AggregateOp, str | None]]


@dataclasses.dataclass(frozen=True)
class GroupFieldBatch:
    """One ``<res>_groups`` page as a caller-declared group field sees it.

    ``groups`` are the page's ``<res>_group`` objects in response order, with
    their typed ``key`` (after the group-key encoders) and ``aggregate``.
    ``where`` is the groups root's ``where`` argument; ``group_by`` is the
    translated ``[(path, granularity)]`` the library grouped by (granularity a
    ``TimeGranularity`` / ``NumberGranularity`` member or ``None``).
    ``arguments`` are this field's arguments as the client selected them, and
    ``info`` is the resolve info of the page's first group that resolves the
    field, so a callback can optimize a read for the field's own selection.
    """

    info: strawberry.Info
    groups: Sequence[Any]
    where: Any
    group_by: Sequence[tuple[str, Any]]
    arguments: Mapping[str, Any]


@dataclasses.dataclass(frozen=True)
class GroupFieldInputs:
    """The resource's own generated inputs a group field's arguments may use.

    ``order_by`` is ``<res>_order_by`` (``None`` for a resource without one),
    so a field can take the ordering its list root takes.
    """

    order_by: type | None


@dataclasses.dataclass(frozen=True)
class GroupField:
    """A caller-declared ``<res>_group`` field filled for a whole page at once.

    ``type`` annotates the field's value; ``arguments`` declares its GraphQL
    arguments as ``name -> annotation`` with optional ``defaults``, or as a
    callable receiving the resource's :class:`GroupFieldInputs` and returning
    that mapping. Wire names are the declared names verbatim. ``resolve``
    runs once per groups page and selected response name, the first time a
    group of that page resolves the field, and returns one value per group in
    ``batch.groups`` order; its outcome, a raised error or a wrong number of
    values included, is kept for every group of that page. The adapter stays
    permission-naive: row scoping is the callback's own read.
    """

    type: Any
    resolve: Callable[[GroupFieldBatch], Sequence[Any]]
    arguments: (
        Mapping[str, Any] | Callable[[GroupFieldInputs], Mapping[str, Any]]
    ) = dataclasses.field(default_factory=dict)
    defaults: Mapping[str, Any] = dataclasses.field(default_factory=dict)
    description: str | None = None


@dataclasses.dataclass
class _GroupPage:
    """One ``<res>_groups`` resolution: its groups and each field's outcome."""

    where: Any
    group_by: Sequence[tuple[str, Any]]
    groups: list[Any] = dataclasses.field(default_factory=list)
    outcomes: dict[tuple[str, str], Sequence[Any] | Exception] = (
        dataclasses.field(default_factory=dict)
    )


# Private group members: GraphQL reserves ``__`` names, so no caller field can
# collide with them.
_PAGE = "__group_page"
_INDEX = "__group_index"


def _group_field_resolver(
    name: str, declared: GroupField, parameters: Sequence[Parameter]
) -> Any:
    """Resolve one declared group field from its page's single batch."""

    def resolve(self: Any, info: strawberry.Info, **selected: Any) -> Any:
        page: _GroupPage = getattr(self, _PAGE)
        key = (name, str(info.path.key))
        if key not in page.outcomes:
            outcome: Sequence[Any] | Exception
            try:
                outcome = declared.resolve(
                    GroupFieldBatch(
                        info=info,
                        groups=page.groups,
                        where=page.where,
                        group_by=page.group_by,
                        arguments=selected,
                    )
                )
            except Exception as error:  # noqa: BLE001 - re-raised per group
                outcome = error
            else:
                if len(outcome) != len(page.groups):
                    outcome = ValueError(
                        f"Group field {name!r} returned {len(outcome)} values "
                        f"for {len(page.groups)} groups."
                    )
            page.outcomes[key] = outcome
        outcome = page.outcomes[key]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome[getattr(self, _INDEX)]

    resolve.__signature__ = Signature(  # type: ignore[attr-defined]
        [
            Parameter("self", Parameter.POSITIONAL_OR_KEYWORD, annotation=Any),
            Parameter(
                "info",
                Parameter.POSITIONAL_OR_KEYWORD,
                annotation=strawberry.Info,
            ),
            *parameters,
        ],
        return_annotation=declared.type,
    )
    resolve.__annotations__ = {
        "self": Any,
        "info": strawberry.Info,
        **{parameter.name: parameter.annotation for parameter in parameters},
        "return": declared.type,
    }
    resolve.__name__ = name
    return resolve


def _group_field(
    name: str, declared: GroupField, inputs: GroupFieldInputs
) -> Any:
    arguments = (
        declared.arguments(inputs)
        if callable(declared.arguments)
        else declared.arguments
    )
    parameters = caller_parameters(
        arguments,
        surface=f"group field {name!r} arguments",
        defaults=declared.defaults,
    )
    field = strawberry.field(
        resolver=django_resolver(
            _group_field_resolver(name, declared, parameters)
        ),
        name=name,
        description=declared.description,
    )
    for argument in field.arguments:
        argument.graphql_name = argument.python_name
    return field


class GroupExecutor(Protocol):
    """Runs one translated grouped request against a resource's rows.

    ``rows`` returns ``compute_aggregation``-shaped result rows for one page;
    ``count`` returns the exact, unpaginated group cardinality for the same
    ``where`` / ``group_by`` / ``having``. Both receive the already-translated
    aggregate-owner arguments, so executors never parse wire inputs.
    """

    def rows(
        self,
        info: strawberry.Info,
        where: Any,
        spec: Spec,
        requested: Requested,
        having: dict[str, Any],
        order_by: list[tuple[str, str, str | None]],
        limit: int | None,
        offset: int,
    ) -> list[dict[str, Any]]: ...

    def count(
        self,
        info: strawberry.Info,
        where: Any,
        spec: Spec,
        requested: Requested,
        having: dict[str, Any],
    ) -> int: ...


def validate_group_key_encoders(
    groupable: list[str] | None,
    group_key_encoders: Mapping[str, Callable[[Any], Any]] | None,
) -> dict[str, Callable[[Any], Any]]:
    """Copy and validate ``group_key_encoders`` against ``groupable``.

    Each key must name a declared groupable path and map to a callable. The
    single check both resource builders run at construction.
    """
    encoders = dict(group_key_encoders or {})
    for path, encoder in encoders.items():
        if path not in (groupable or []) or not callable(encoder):
            raise ValueError(
                f"Group-key encoder {path!r} must name a declared groupable "
                "path and be callable"
            )
    return encoders


class _QuerysetGroups:
    """Group a model resource's filtered queryset in SQL."""

    def __init__(
        self,
        *,
        builder: Any,
        filtered_queryset: Callable[[strawberry.Info, Any], QuerySet[Any]],
        get_group_by_expressions: GroupByExpressionProvider | None,
    ) -> None:
        self._builder = builder
        self._filtered = filtered_queryset
        self._expressions = get_group_by_expressions

    def _source(
        self, info: strawberry.Info, where: Any, spec: Spec
    ) -> tuple[QuerySet[Any], Mapping[str, Combinable] | None]:
        qs = self._filtered(info, where)
        if self._expressions is None:
            return qs, None
        return qs, self._expressions(info, qs, spec)

    def rows(
        self,
        info: strawberry.Info,
        where: Any,
        spec: Spec,
        requested: Requested,
        having: dict[str, Any],
        order_by: list[tuple[str, str, str | None]],
        limit: int | None,
        offset: int,
    ) -> list[dict[str, Any]]:
        qs, expressions = self._source(info, where, spec)
        rows: list[dict[str, Any]] = compute_aggregation(
            qs,
            group_by=spec,
            aggregates=requested,
            group_by_expressions=expressions,
            having=having,
            order_by=order_by,
            limit=limit,
            offset=offset,
            json_paths=self._builder.json_paths,
        )
        return rows

    def count(
        self,
        info: strawberry.Info,
        where: Any,
        spec: Spec,
        requested: Requested,
        having: dict[str, Any],
    ) -> int:
        qs, expressions = self._source(info, where, spec)
        return int(
            self._builder.count_groups(
                qs,
                spec,
                requested,
                having,
                group_by_expressions=expressions,
            )
        )


class _RowSourceGroups:
    """Group the rows a ``RowSource`` returns for ``where``, in memory.

    ``rows(info, where)`` is the source's full, ``where``-filtered row set
    (unordered, unpaged); ``row_model`` declares the groupable columns
    (``strawberry_django_aggregates.make_row_model``). Grouping, HAVING,
    ordering and paging are the aggregate owner's ``compute_row_aggregation``.
    """

    def __init__(
        self,
        *,
        rows: Callable[[strawberry.Info, Any], list[Any]],
        row_model: Any,
    ) -> None:
        self._rows = rows
        self._row_model = row_model

    def rows(
        self,
        info: strawberry.Info,
        where: Any,
        spec: Spec,
        requested: Requested,
        having: dict[str, Any],
        order_by: list[tuple[str, str, str | None]],
        limit: int | None,
        offset: int,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = compute_row_aggregation(
            self._rows(info, where),
            model=self._row_model,
            group_by=spec,
            aggregates=requested,
            having=having,
            order_by=order_by,
            limit=limit,
            offset=offset,
        )
        return rows

    def count(
        self,
        info: strawberry.Info,
        where: Any,
        spec: Spec,
        requested: Requested,
        having: dict[str, Any],
    ) -> int:
        return len(
            compute_row_aggregation(
                self._rows(info, where),
                model=self._row_model,
                group_by=spec,
                aggregates=requested,
                having=having,
            )
        )


def make_groups_field(
    *,
    builder: Any,
    built: Any,
    resource_name: str,
    filter_type: type,
    get_queryset: Any,
    prepare_filter_queryset: Callable[[QuerySet[Any], Q], QuerySet[Any]]
    | None = None,
    id_decode: Any = None,
    id_column: str = "pk",
    field_decoders: Any = None,
    max_groups: int | None = None,
    group_key_encoders: Mapping[str, Callable[[Any], Any]] | None = None,
    filter_lookups: Mapping[str, tuple[str, bool]] | None = None,
    get_group_by_expressions: GroupByExpressionProvider | None = None,
    group_fields: Mapping[str, GroupField] | None = None,
    order_by_type: type | None = None,
) -> tuple[Any, Any, list[type]]:
    """Return grouped row/count fields + the generated group types.

    PREVIEW / NDC-shaped (see the module docstring). ``builder`` / ``built``
    are the model's :class:`~strawberry_django_aggregates.AggregateBuilder`
    and its ``BuiltAggregates`` — ``built.aggregate_type`` is the SAME free
    aggregate the ``<res>_aggregate`` container exposes, so the grouped
    aggregate is not a second type. Emits a ``<res>_group { key:
    <Model>GroupKey!, aggregate: <Model>Aggregate! }`` container under a
    ``<res>_groups(group_by, where, having, order_by, limit, offset)`` root
    plus its exact, unpaginated ``<res>_groups_count`` companion.
    ``group_fields`` declares extra group fields (:class:`GroupField`);
    ``order_by_type`` is the resource's ``<res>_order_by`` they may take.
    """
    filter_lookups = _filter_lookups(filter_lookups)

    def filtered_queryset(info: strawberry.Info, where: Any) -> QuerySet[Any]:
        qs: QuerySet[Any] = get_queryset(info)
        return filter_queryset(
            qs,
            where_to_q(
                where,
                id_column=id_column,
                id_decode=id_decode,
                field_decoders=field_decoders,
                lookups=filter_lookups,
            ),
            prepare_queryset=prepare_filter_queryset,
        )

    return build_groups_fields(
        builder=builder,
        built=built,
        aggregate_type=built.aggregate_type,
        resource_name=resource_name,
        filter_type=filter_type,
        executor=_QuerysetGroups(
            builder=builder,
            filtered_queryset=filtered_queryset,
            get_group_by_expressions=get_group_by_expressions,
        ),
        max_groups=max_groups,
        group_key_encoders=group_key_encoders,
        group_fields=group_fields,
        order_by_type=order_by_type,
    )


def build_groups_fields(
    *,
    builder: Any,
    built: Any,
    aggregate_type: type,
    resource_name: str,
    filter_type: type,
    executor: GroupExecutor,
    max_groups: int | None = None,
    group_key_encoders: Mapping[str, Callable[[Any], Any]] | None = None,
    group_fields: Mapping[str, GroupField] | None = None,
    order_by_type: type | None = None,
) -> tuple[Any, Any, list[type]]:
    """Assemble ``<res>_groups`` / ``<res>_groups_count`` over an executor.

    The one implementation of the grouped wire surface. ``builder`` /
    ``built`` provide the typed key, group-by spec, having and group-order
    types plus the translators and key shaper; ``aggregate_type`` is the
    resource's own aggregate payload (the same type ``<res>_aggregate``
    exposes), filled by ``shape_aggregate_row``. ``executor`` runs the
    translated request (:class:`GroupExecutor`). ``group_fields`` adds
    caller-declared ``<res>_group`` fields, each filled for the whole page by
    one batch callback; ``key`` / ``aggregate`` fail at build, as does a name
    that is not a GraphQL field name. Generated wire names are snake-pinned
    here: the container without descending into a caller-declared field type.
    """
    group_key_encoders = dict(group_key_encoders or {})
    group_fields = dict(group_fields or {})
    if clash := sorted({"key", "aggregate"} & group_fields.keys()):
        raise ValueError(
            f"Group fields {clash} collide with {resource_name}_group fields"
        )
    for name in group_fields:
        if not name.isidentifier() or iskeyword(name) or name.startswith("__"):
            raise TypeError(f"Group field name {name!r} is not a field name")
    module = _host_module(resource_name)
    group_key_type = built.group_key_type
    group_by_spec = built.group_by_spec
    having_input = built.having_input
    group_order_input = built.group_order_input

    annotations: dict[str, Any] = {
        "key": group_key_type,
        "aggregate": aggregate_type,
    }
    namespace: dict[str, Any] = {
        "__module__": module.__name__,
        "__annotations__": annotations,
    }
    if group_fields:
        # Every constructed group of such a type receives its page.
        annotations[_PAGE] = strawberry.Private[_GroupPage]
        annotations[_INDEX] = strawberry.Private[int]
        namespace[_PAGE] = None
        namespace[_INDEX] = 0
        inputs = GroupFieldInputs(order_by=order_by_type)
        for name, declared in group_fields.items():
            namespace[name] = _group_field(name, declared, inputs)
    group_type = strawberry.type(
        type(f"{resource_name}_group", (), namespace),
        name=f"{resource_name}_group",
    )
    setattr(module, f"{resource_name}_group", group_type)

    def resolve_groups(
        self: Any,
        info: strawberry.Info,
        group_by: list[Any],
        where: Any = None,
        having: Any = None,
        order_by: Any = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[Any]:
        del self
        validate_pagination(limit, offset)
        # Owner-translated: wire inputs → aggregate-owner arguments. The
        # adapter never re-implements the spec / granularity / having parsing.
        spec = builder.translate_group_by(group_by)
        requested = _requested_group_ops(info, builder.json_paths)
        having_dict = builder.translate_having(having, requested)
        order_terms = builder.translate_order_by(order_by, spec, requested)
        rows = executor.rows(
            info,
            where,
            spec,
            requested,
            having_dict,
            order_terms,
            capped_limit(limit, max_groups),
            offset or 0,
        )
        page = _GroupPage(where=where, group_by=spec) if group_fields else None
        groups = [
            group_type(
                key=builder.shape_group_key(
                    group_key_type,
                    row,
                    spec,
                    value_encoders=group_key_encoders,
                ),
                aggregate=shape_aggregate_row(
                    aggregate_type,
                    row,
                    requested,
                    json_paths=builder.json_paths,
                ),
                **({_PAGE: page, _INDEX: index} if page is not None else {}),
            )
            for index, row in enumerate(rows)
        ]
        if page is not None:
            page.groups = groups
        return groups

    resolve_groups.__annotations__ = {
        "self": Any,
        "info": strawberry.Info,
        "group_by": list[group_by_spec],  # type: ignore[valid-type]
        "where": filter_type | None,
        "having": having_input | None,
        "order_by": list[group_order_input] | None,  # type: ignore[valid-type]
        "limit": int | None,
        "offset": int | None,
        "return": list[group_type],  # type: ignore[valid-type]
    }

    def resolve_groups_count(
        self: Any,
        info: strawberry.Info,
        group_by: list[Any],
        where: Any = None,
        having: Any = None,
    ) -> int:
        del self
        spec = builder.translate_group_by(group_by)
        requested: Requested = [(AggregateOp.COUNT, None)]
        having_dict = builder.translate_having(having, requested)
        return executor.count(info, where, spec, requested, having_dict)

    resolve_groups_count.__annotations__ = {
        "self": Any,
        "info": strawberry.Info,
        "group_by": list[group_by_spec],  # type: ignore[valid-type]
        "where": filter_type | None,
        "having": having_input | None,
        "return": int,
    }
    types = [
        group_type,
        group_key_type,
        group_by_spec,
        having_input,
        group_order_input,
    ]
    # The query walk reaches the group container and key but not the having /
    # order_by INPUT types (``count_gt`` would camelCase). The container is
    # pinned without descending: a caller-declared field may return a
    # caller-owned type that keeps its own names.
    for generated in types:
        pin_snake_wire_names(generated, recursive=generated is not group_type)
    return (
        strawberry.field(
            resolver=django_resolver(resolve_groups),
            name=f"{resource_name}_groups",
        ),
        strawberry.field(
            resolver=django_resolver(resolve_groups_count),
            name=f"{resource_name}_groups_count",
        ),
        types,
    )


def _requested_group_ops(
    info: strawberry.Info,
    json_paths: Mapping[str, str] | None = None,
) -> list[tuple[Any, str | None]]:
    """The ``(op, field)`` pairs the client selected under ``aggregate``.

    Gathers the op blocks nested under the ``<res>_groups`` selection's
    ``aggregate`` sub-field and maps them via the shared
    :func:`~strawberry_django_hasura.aggregation._ops_from_aggregate_blocks`
    — same op vocabulary, ``count``-always, and first-seen dedupe
    (deterministic SQL) as the free ``<res>_aggregate`` resolver.
    """
    blocks = [
        agg_field
        for top in info.selected_fields
        for field in _selected_fields(top.selections)
        if field.name == "aggregate"
        for agg_field in _selected_fields(field.selections)
    ]
    return _ops_from_aggregate_blocks(blocks, json_paths)


def _host_module(name: str) -> types.ModuleType:
    """A real, importable namespace for the generated ``<res>_group`` type.

    Strawberry reads a type's ``__module__`` when registering it; giving the
    dynamically-built group type a stable synthetic module (mirroring
    ``resource._host_module``) keeps its identity stable across builds. Unlike
    the resource's inputs, the group type has no string forward refs to resolve
    — its ``key`` / ``aggregate`` annotations are already-built type objects.
    """
    module_name = f"{__name__}._generated.{name}"
    module = sys.modules.get(module_name)
    if module is None:
        module = types.ModuleType(module_name)
        sys.modules[module_name] = module
    return module
