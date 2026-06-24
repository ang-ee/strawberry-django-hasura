"""One-call ``hasura_resource(...)`` — the full Hasura surface for a model.

The five surfaces (``comparisons`` / ``filtering`` / ``ordering`` /
``connection`` / ``mutations`` / ``aggregation``) are model-independent
primitives. Composing them into a working resource is otherwise hand-wiring
per model: declare the ``<res>_bool_exp`` / ``<res>_order_by`` /
``<res>_insert_input`` / ``<res>_set_input`` / ``<res>_pk_columns_input``
inputs, the ``<res>`` / ``<res>_aggregate`` / ``<res>_by_pk`` query fields, the
``insert_<res>_one`` / ``update_<res>_by_pk`` / ``delete_<res>_by_pk``
mutations, and the free ``<Model>Aggregate`` container — and then pin every
snake_case wire name.

:func:`hasura_resource` assembles all of that from one call. It owns only
*composition + naming*: each fact still lives with its owner — the comparison /
order scalar comes from the **Django field**, filtering / ordering / paging /
aggregation are the existing primitives unchanged, row scoping is the caller's
``get_queryset``, authorized writes are the caller's ``write_backend``, and the
sqid⇄pk boundary is the caller's ``id_decode``. The builder adds no rebac /
Angee imports.

**Snake naming is baked in.** A Hasura-default schema keeps snake_case on the
wire, but a consuming schema (e.g. Angee) installs the default *camelCase*
converter for the whole schema and has no per-surface seam, so
:func:`~strawberry_django_hasura.naming.hasura_config` cannot be used there.
The builder therefore pins each generated field's and argument's
``graphql_name`` to its snake_case python name — including the generated
``<Model>Aggregate`` type's field names (the aggregates compiler maps a
selected measure name straight back to ``model._meta.get_field``, so a
camelCased aggregate field breaks at runtime). ``hasura_config()`` stays an
optional convenience for a schema dedicated to a single dialect.
"""

from __future__ import annotations

import datetime
import decimal
import sys
import types
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, cast

import strawberry
from django.db.models import Model, QuerySet
from strawberry import UNSET
from strawberry.types import get_object_definition
from strawberry_django.fields.types import field_type_map
from strawberry_django_aggregates import AggregateBuilder

from .aggregation import make_aggregate_resolver
from .comparisons import (
    BooleanComparison,
    DateTimeComparison,
    FloatComparison,
    IDComparison,
    IntComparison,
    JSONComparison,
    StringComparison,
)
from .connection import make_aggregate_container, paginate
from .filtering import where_to_q
from .grouping import make_groups_field
from .mutations import input_to_dict
from .ordering import OrderBy, apply_ordering

# The column's python type is the **Django field's** fact: strawberry-django
# owns ``model field -> python type`` in ``field_type_map`` (keyed by field
# class), and the node type is built from it. We ask that owner rather than
# re-deriving a parallel scalar table (which drifts). ``_column_python_type``
# walks the field's MRO so a subclass (``EmailField`` → ``CharField``) inherits
# its base mapping.
#
# This map is the one fact the *adapter* owns: the Hasura ``*_comparison_exp``
# input for each python scalar (its filter operator vocabulary). ``strawberry``
# ``ID`` is the pk / public ``id`` surface (String-typed args refine's
# ``idType`` binds — see ``comparisons``/``filtering``).
_COMPARISON_FOR_TYPE: dict[Any, type] = {
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
    strawberry.scalars.JSON: JSONComparison,
}

#: The fixed refine ``idType`` wire field name. refine derives the id variable
#: (``$id``) from this name, so its comparison is always ``IDComparison`` and
#: its pk-arg surface is String-typed — independent of the Django ``id_column``
#: it resolves against (see ``CONTRACT.md`` — sqid / idType boundary).
_ID_WIRE_NAME = "id"


class WriteBackend(Protocol):
    """The caller-supplied authorized-write seam for the mutation surface.

    Persistence (and its authorization — REBAC gates, ``full_clean``, relation
    coercion) belongs to the model / the consuming app, not this library. Each
    Hasura write dispatches to one method here with the already-decoded input;
    the toy demo wraps the bare ORM, a real consumer wraps its CRUD machinery.
    ``delete`` returns the deleted instance (or ``None``) so the Hasura
    ``delete_<res>_by_pk`` response can resolve the removed row.
    """

    def create(self, info: strawberry.Info, data: dict[str, Any]) -> Any: ...

    def update(
        self, info: strawberry.Info, pk: str, data: dict[str, Any]
    ) -> Any: ...

    def delete(self, info: strawberry.Info, pk: str) -> Any | None: ...


@dataclass(frozen=True)
class HasuraResource:
    """The assembled Hasura surface for one model — drop into a schema bucket.

    ``query`` / ``mutation`` carry the root field holders; ``types`` carries
    the generated container + ``<Model>Aggregate`` and the input types. A
    schema bucket merges these as the hand-wired resource exposed them.
    """

    query: type
    mutation: type
    types: list[type]


def _pin_snake_wire_names(
    strawberry_type: type,
    seen: set[int] | None = None,
) -> None:
    """Pin each field's GraphQL wire name to its snake_case python name.

    Walks the type (and its nested object types — the ``<Model>Aggregate``'s
    ``sum`` / ``avg`` / … measure types) and, where strawberry would otherwise
    camelCase a snake_case python identifier, pins the verbatim snake name. An
    explicit ``strawberry.field(name=...)`` already set a ``graphql_name`` and
    is left untouched. This is the same wire effect as
    :func:`~strawberry_django_hasura.naming.hasura_config`, scoped to the types
    this builder generates, so a consumer on a camelCase schema gets snake_case
    wire names for this resource without a schema-wide converter.
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
            _pin_snake_wire_names(cast("type", inner), seen)


def _host_module(name: str) -> types.ModuleType:
    """A synthetic module to host this resource's generated types.

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


def _input_type(
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
    resolve.
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
    # Pin the snake_case column names this input declares (a default schema
    # would camelCase ``word_count`` → ``wordCount``). The nested
    # ``*_comparison_exp`` / ``order_by`` members already carry explicit wire
    # names, so the ``graphql_name is None`` guard leaves them untouched.
    _pin_snake_wire_names(built)
    return built


def _column_python_type(field: Any) -> Any:
    """The python type a Django column carries — asked of strawberry-django.

    Defers to the owner (``field_type_map``) instead of re-listing scalars, so
    the insert / ``_set`` input fields match the node type by construction. The
    map is keyed by exact field class; walk the MRO so a subclass inherits its
    base mapping (``EmailField`` → ``CharField`` → ``str``). A field type the
    owner does not map raises rather than silently degrading to ``str`` (the
    library's fail-fast stance — see ``filtering.comparison_to_q``).
    """
    if getattr(field, "many_to_one", False):
        return _column_python_type(field.target_field)
    for klass in type(field).__mro__:
        if klass in field_type_map:
            return field_type_map[klass]
    raise TypeError(
        f"field {field.name!r} ({type(field).__name__}) has no "
        "strawberry-django type mapping; it cannot be exposed as a Hasura "
        "comparison / writable column"
    )


def _comparison_for(
    field: Any,
    *,
    public_id: bool = False,
) -> type:
    """The ``*_comparison_exp`` input for a scalar Django field.

    The column's python type comes from the owner; this maps that scalar onto
    the adapter's own Hasura comparison vocabulary.
    """
    python = strawberry.ID if public_id else _column_python_type(field)
    comparison = _COMPARISON_FOR_TYPE.get(python)
    if comparison is None:
        raise TypeError(
            f"field {field.name!r} (python type {python!r}) has no Hasura "
            "comparison input; it is not a filterable scalar"
        )
    return comparison


def _writable_fields(
    model: type[Model],
    id_column: str,
    writable: list[str] | None = None,
) -> list[Any]:
    """The editable, non-pk, non-auto fields (insert / ``_set``).

    The writable allowlist is a fact of the Django model. Concrete columns are
    settable from the client when editable, not the primary key, not the public
    ``id`` column, and not an ``auto_now``/``auto_now_add`` stamp. Many-to-many
    relation arrays are settable too: they are not columns, but Django's native
    mutation resolver owns applying those relation lists after the row exists.
    The server owns fields excluded here. A caller may pass an explicit
    ``writable`` list to mirror Hasura permissions; invalid names fail fast
    instead of being silently skipped.
    """
    out: list[Any] = []
    fields = (
        [model._meta.get_field(name) for name in writable]
        if writable is not None
        else model._meta.get_fields()
    )
    for field in fields:
        reason = _not_writable_reason(field, id_column)
        if reason is not None:
            if writable is not None:
                raise TypeError(
                    f"field {field.name!r} cannot be exposed as a Hasura "
                    f"writable column: {reason}"
                )
            continue
        out.append(field)
    return out


def _not_writable_reason(field: Any, id_column: str) -> str | None:
    if getattr(field, "many_to_many", False):
        return None
    if not getattr(field, "concrete", False):
        return "it is not a concrete column"
    if getattr(field, "primary_key", False):
        return "it is the primary key"
    if field.name == id_column:
        return "it is the public id column"
    if not getattr(field, "editable", False):
        return "it is not editable"
    if getattr(field, "auto_now", False) or getattr(
        field, "auto_now_add", False
    ):
        return "it is an automatic timestamp"
    return None


def _writable_python_type(
    field: Any,
    *,
    public_id: bool = False,
) -> Any:
    """Return the GraphQL input type for one writable model column."""

    if getattr(field, "many_to_many", False):
        item_type = (
            strawberry.ID
            if public_id
            else _column_python_type(field.target_field)
        )
        return types.GenericAlias(list, (item_type,))
    return strawberry.ID if public_id else _column_python_type(field)


def _enabled_operations(
    *,
    insert: bool,
    update: bool,
    delete: bool,
) -> tuple[str, ...]:
    """Return enabled mutation operation names in stable Hasura order."""

    return tuple(
        name
        for name, enabled in (
            ("insert", insert),
            ("update", update),
            ("delete", delete),
        )
        if enabled
    )


def hasura_resource(  # noqa: PLR0913 — declarative builder: one knob per facet
    node: type,
    *,
    model: type[Model],
    name: str | None = None,
    filterable: list[str],
    sortable: list[str],
    aggregatable: list[str],
    groupable: list[str] | None = None,
    max_groups: int | None = None,
    writable: list[str] | None = None,
    insertable: list[str] | None = None,
    updatable: list[str] | None = None,
    insert: bool = True,
    update: bool = True,
    delete: bool = True,
    field_id_decode: Mapping[str, Callable[[Any], Any]] | None = None,
    get_queryset: Callable[[strawberry.Info], QuerySet[Any]],
    get_aggregate_queryset: (
        Callable[[strawberry.Info], QuerySet[Any]] | None
    ) = None,
    write_backend: WriteBackend,
    id_decode: Callable[[Any], Any] | None = None,
    id_column: str = "pk",
) -> HasuraResource:
    """Assemble the full Hasura surface for ``model`` in one call.

    ``node`` is the ``strawberry_django.type`` for the rows. ``name`` is the
    resource stem (the plural Hasura name — ``"notes"``); it defaults to the
    model's lower-cased name. ``filterable`` / ``sortable`` / ``aggregatable``
    are the column allowlists for ``<res>_bool_exp`` / ``<res>_order_by`` /
    ``<Model>Aggregate``. ``groupable`` enables the optional NDC-shaped
    ``<res>_groups`` companion root; ``max_groups`` caps its offset page (a
    high-cardinality dimension would otherwise pull every group — default
    ``None`` is uncapped; pass ``order_by`` for stable pages). ``writable``
    mirrors Hasura field
    permissions for insert / ``_set`` inputs (default: editable concrete model
    columns plus editable many-to-many relation arrays). ``insertable`` and
    ``updatable`` override that shared allowlist for insert and update
    separately. ``insert`` / ``update`` / ``delete``
    mirror Hasura table mutation operation permissions: disabling one removes
    its root and the input types used only by that operation.
    ``field_id_decode`` marks non-``id`` scalar fields whose Hasura operands
    are public ids and must be decoded before the Django lookup, e.g. a
    foreign-key column exposed as a public id.
    ``get_queryset(info)`` returns the already row-scoped base source for
    list/detail reads. ``get_aggregate_queryset(info)`` can override the source
    used by aggregate math and groups when a consumer needs a different
    queryset policy there; aggregate ``nodes`` still use ``get_queryset``.
    ``write_backend`` is the authorized-write seam (:class:`WriteBackend`).
    ``id_decode`` / ``id_column`` map the public ``id`` operand onto the ORM
    lookup for the sqid boundary (defaults to a raw-pk project).

    Returns a :class:`HasuraResource` whose ``query`` / ``mutation`` /
    ``types`` drop into a schema bucket. Every generated wire name (roots,
    args, input fields, and the ``<Model>Aggregate`` field names) is pinned
    snake_case, so the resource is correct on a camelCase schema without
    ``hasura_config()``.
    """
    res = name or model.__name__.lower()
    public_id_fields = frozenset(field_id_decode or {})
    operations = _enabled_operations(
        insert=insert,
        update=update,
        delete=delete,
    )
    # The ``<Model>Aggregate`` prefix is the node's GraphQL name (``Note``,
    # owned by the node type), not the Django class name (``NoteModel``).
    node_definition = get_object_definition(node)
    aggregate_prefix = (
        node_definition.name if node_definition is not None else model.__name__
    )
    module = _host_module(res)
    get_field = model._meta.get_field

    # --- where / order_by / mutation inputs (derived from the Django fields) -
    bool_exp_name = f"{res}_bool_exp"
    # ``id`` is the fixed refine ``idType`` wire name (not the Django column,
    # which is ``id_column``): refine declares the id variable ``$id``, so its
    # comparison is always ``IDComparison`` (the String-typed pk surface — see
    # ``comparisons`` / ``CONTRACT.md``). It is also often a resolver-only
    # public field, so it never reaches ``get_field``.
    bool_exp_ann: dict[str, Any] = {
        col: (
            IDComparison
            if col == _ID_WIRE_NAME
            else _comparison_for(
                get_field(col),
                public_id=col in public_id_fields,
            )
        )
        | None
        for col in filterable
    }
    bool_exp_defaults: dict[str, Any] = dict.fromkeys(filterable, UNSET)
    # Self-referential boolean composition (resolved via ``module`` globals).
    bool_exp_ann |= {
        "and_": f"list[{bool_exp_name}] | None",
        "or_": f"list[{bool_exp_name}] | None",
        "not_": f"{bool_exp_name} | None",
    }
    bool_exp_fields = {
        "and_": strawberry.field(name="_and", default=UNSET),
        "or_": strawberry.field(name="_or", default=UNSET),
        "not_": strawberry.field(name="_not", default=UNSET),
    }
    bool_exp = _input_type(
        bool_exp_name,
        bool_exp_ann,
        module=module,
        defaults=bool_exp_defaults,
        fields=bool_exp_fields,
    )

    order_by_input = _input_type(
        f"{res}_order_by",
        dict.fromkeys(sortable, OrderBy | None),
        module=module,
        defaults=dict.fromkeys(sortable, UNSET),
    )

    insert_fields = _writable_fields(
        model,
        id_column,
        insertable if insertable is not None else writable,
    )
    set_fields = _writable_fields(
        model,
        id_column,
        updatable if updatable is not None else writable,
    )
    # insert: required only when the model field has no default and is not
    # nullable. Columns with Django defaults are omitted from the resolver
    # input and let the model apply its default; the GraphQL SDL does not
    # mirror Python default values (especially mutable / JSON defaults).
    insert_input: type | None = None
    if insert:
        insert_ann: dict[str, Any] = {}
        insert_defaults: dict[str, Any] = {}
        for field in insert_fields:
            python_type = _writable_python_type(
                field,
                public_id=field.name in public_id_fields,
            )
            optional_on_insert = (
                field.has_default()
                or getattr(field, "null", False)
                or getattr(field, "blank", False)
            )
            insert_ann[field.name] = (
                python_type | None if optional_on_insert else python_type
            )
            if optional_on_insert:
                insert_defaults[field.name] = UNSET
        insert_input = _input_type(
            f"{res}_insert_input",
            insert_ann,
            module=module,
            defaults=insert_defaults,
        )

    set_input: type | None = None
    if update:
        set_input = _input_type(
            f"{res}_set_input",
            {
                field.name: _writable_python_type(
                    field,
                    public_id=field.name in public_id_fields,
                )
                | None
                for field in set_fields
            },
            module=module,
            defaults={field.name: UNSET for field in set_fields},
        )

    pk_columns_input: type | None = None
    if update:
        pk_columns_input = _input_type(
            f"{res}_pk_columns_input",
            {"id": str},
            module=module,
        )

    aggregate_get_queryset = get_aggregate_queryset or get_queryset

    def _filtered(
        info: strawberry.Info,
        where: Any,
        source: Callable[[strawberry.Info], QuerySet[Any]],
    ) -> QuerySet[Any]:
        # ``source(info)`` is the caller's already row-scoped source; the
        # resource applies the Hasura ``where`` on top.
        return source(info).filter(
            where_to_q(
                where,
                id_column=id_column,
                id_decode=id_decode,
                field_decoders=field_id_decode,
            )
        )

    def filtered(info: strawberry.Info, where: Any) -> QuerySet[Any]:
        return _filtered(info, where, get_queryset)

    def filtered_aggregate(info: strawberry.Info, where: Any) -> QuerySet[Any]:
        return _filtered(info, where, aggregate_get_queryset)

    # --- the free aggregate (+ optional grouped surface) ---------------------
    # One ``AggregateBuilder`` produces BOTH the free ``<Model>Aggregate`` and
    # — when ``groupable`` is set — the typed ``<Model>GroupKey`` / group-by
    # spec / having / group-order types the grouping surface composes, sharing
    # the SAME aggregate type (never a second ``<Model>Aggregate``). The
    # aggregate stays free: zero reshape.
    agg_builder = AggregateBuilder(
        model=model,
        name_prefix=aggregate_prefix,
        aggregate_fields=aggregatable,
        group_by_fields=groupable or None,
    )
    agg_built = agg_builder.build()
    aggregate_type = cast("type", agg_built.aggregate_type)
    _pin_snake_wire_names(aggregate_type)
    aggregate_resolver = make_aggregate_resolver(aggregate_type)
    container = make_aggregate_container(
        f"{res}_aggregate",
        node,
        aggregate_type,
        filtered_queryset=filtered_aggregate,
        filtered_nodes_queryset=filtered,
        aggregate_resolver=aggregate_resolver,
    )
    groups_field: Any = None
    groups_types: list[type] = []
    if groupable:
        groups_field, groups_types = make_groups_field(
            builder=agg_builder,
            built=agg_built,
            resource_name=res,
            filter_type=bool_exp,
            get_queryset=aggregate_get_queryset,
            id_decode=id_decode,
            id_column=id_column,
            field_decoders=field_id_decode,
            max_groups=max_groups,
        )
        # Pin snake_case on the generated group types — the query walk reaches
        # the group container + key (a return type) but not the ``having`` /
        # ``order_by`` INPUT types (e.g. ``count_gt`` would camelCase).
        for grouped in groups_types:
            _pin_snake_wire_names(grouped)

    # --- root query fields ---------------------------------------------------
    def resolve_list(
        self: Any,
        info: strawberry.Info,
        where: Any = None,
        order_by: Any = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[Any]:
        qs = apply_ordering(filtered(info, where), order_by)
        return list(paginate(qs, limit, offset))

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
        lookup = id_decode(id) if id_decode is not None else id
        return get_queryset(info).filter(**{id_column: lookup}).first()

    resolve_by_pk.__annotations__ = {
        "self": Any,
        "info": strawberry.Info,
        "id": str,
        "return": node | None,
    }

    query_fields = {
        res: strawberry.field(resolver=resolve_list, name=res),
        f"{res}_aggregate": strawberry.field(
            resolver=resolve_aggregate, name=f"{res}_aggregate"
        ),
        f"{res}_by_pk": strawberry.field(
            resolver=resolve_by_pk, name=f"{res}_by_pk"
        ),
    }
    if groups_field is not None:
        query_fields[f"{res}_groups"] = groups_field
    query = strawberry.type(type(f"{res}__query", (), query_fields))

    # --- root mutation fields ------------------------------------------------
    mutation_fields: dict[str, Any] = {}
    if "insert" in operations:
        assert insert_input is not None

        def resolve_insert(
            self: Any,
            info: strawberry.Info,
            object: Any,
        ) -> Any:
            return write_backend.create(info, input_to_dict(object))

        resolve_insert.__annotations__ = {
            "self": Any,
            "info": strawberry.Info,
            "object": insert_input,
            "return": node,
        }
        mutation_fields[f"insert_{res}_one"] = strawberry.mutation(
            resolver=resolve_insert,
            name=f"insert_{res}_one",
        )
    if "update" in operations:
        assert pk_columns_input is not None
        assert set_input is not None

        def resolve_update(
            self: Any, info: strawberry.Info, pk_columns: Any, _set: Any
        ) -> Any:
            return write_backend.update(
                info,
                pk_columns.id,
                input_to_dict(_set),
            )

        resolve_update.__annotations__ = {
            "self": Any,
            "info": strawberry.Info,
            "pk_columns": pk_columns_input,
            "_set": set_input,
            "return": node,
        }
        mutation_fields[f"update_{res}_by_pk"] = strawberry.mutation(
            resolver=resolve_update,
            name=f"update_{res}_by_pk",
        )
    if "delete" in operations:

        def resolve_delete(
            self: Any, info: strawberry.Info, id: str
        ) -> Any | None:
            return write_backend.delete(info, id)

        resolve_delete.__annotations__ = {
            "self": Any,
            "info": strawberry.Info,
            "id": str,
            "return": node | None,
        }
        mutation_fields[f"delete_{res}_by_pk"] = strawberry.mutation(
            resolver=resolve_delete,
            name=f"delete_{res}_by_pk",
        )

    mutation = strawberry.type(type(f"{res}__mutation", (), mutation_fields))

    # Pin snake_case on the root holders' fields + arguments (``order_by`` /
    # ``pk_columns`` / ``_set`` would otherwise camelCase on a default schema).
    _pin_snake_wire_names(query)
    _pin_snake_wire_names(mutation)

    return HasuraResource(
        query=query,
        mutation=mutation,
        types=[
            item
            for item in (
                container,
                aggregate_type,
                bool_exp,
                order_by_input,
                insert_input,
                set_input,
                pk_columns_input,
                *groups_types,
            )
            if item is not None
        ],
    )
