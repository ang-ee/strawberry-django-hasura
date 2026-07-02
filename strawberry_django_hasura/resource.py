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

import types
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, cast

import strawberry
from django.db.models import Model, QuerySet
from strawberry import UNSET
from strawberry.types import get_object_definition
from strawberry_django.fields.types import field_type_map
from strawberry_django.optimizer import optimize
from strawberry_django_aggregates import AggregateBuilder

from .aggregation import make_aggregate_resolver
from .comparisons import IDComparison
from .connection import make_aggregate_container, paginate
from .filtering import where_to_q
from .grouping import make_groups_field
from .inputs import (
    ID_WIRE_NAME as _ID_WIRE_NAME,
)
from .inputs import (
    build_bool_exp,
    build_order_by,
    comparison_for_python_type,
)
from .inputs import (
    host_module as _host_module,
)
from .inputs import (
    input_type as _input_type,
)
from .inputs import (
    pin_snake_wire_names as _pin_snake_wire_names,
)
from .mutations import input_to_dict
from .ordering import apply_ordering


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
    A read-only resource (all of insert/update/delete disabled) carries an
    empty mutation holder — it merges to nothing.

    The role-named members expose facts the builder already had while
    assembling the surface. Consumers that need "the filter type" or "the
    insert root name" should read them from the built resource instead of
    re-templating Hasura's naming convention. ``aggregate_container_type`` is
    the Hasura ``<res>_aggregate`` wrapper; ``aggregate_type`` is its inner
    ``aggregate`` payload (the native ``<Model>Aggregate`` on the model path,
    the count-only ``<Node>Aggregate`` on the row-source path).
    """

    query: type
    mutation: type
    types: list[type]
    name: str | None = None
    node_type: type | None = None
    filter_type: type | None = None
    order_by_type: type | None = None
    insert_input_type: type | None = None
    set_input_type: type | None = None
    pk_columns_input_type: type | None = None
    aggregate_container_type: type | None = None
    aggregate_type: type | None = None
    group_type: type | None = None
    group_key_type: type | None = None
    group_by_spec_type: type | None = None
    group_order_type: type | None = None
    having_type: type | None = None
    list_root: str | None = None
    aggregate_root: str | None = None
    detail_root: str | None = None
    groups_root: str | None = None
    insert_one_root: str | None = None
    update_by_pk_root: str | None = None
    delete_by_pk_root: str | None = None
    enabled_operations: tuple[str, ...] = ()
    insertable_fields: tuple[str, ...] = ()
    updatable_fields: tuple[str, ...] = ()


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
    the adapter's own Hasura comparison vocabulary (``inputs``).
    """
    return comparison_for_python_type(
        strawberry.ID if public_id else _column_python_type(field)
    )


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

    # --- where / order_by inputs (derived from the Django fields) ------------
    # ``id`` is the fixed refine ``idType`` wire name (not the Django column,
    # which is ``id_column``): its comparison is always ``IDComparison`` (the
    # String-typed pk surface) and it never reaches ``get_field``. The bool_exp
    # / order_by assembly itself is the model-independent ``inputs`` owner.
    bool_exp = build_bool_exp(
        res,
        {
            col: (
                IDComparison
                if col == _ID_WIRE_NAME
                else _comparison_for(
                    get_field(col), public_id=col in public_id_fields
                )
            )
            for col in filterable
        },
        module,
    )
    order_by_input = build_order_by(res, sortable, module)

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
    group_type: type | None = None
    group_key_type: type | None = None
    group_by_spec_type: type | None = None
    group_order_type: type | None = None
    having_type: type | None = None
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
        group_type = groups_types[0]
        group_key_type = cast("type", agg_built.group_key_type)
        group_by_spec_type = groups_types[2]
        having_type = groups_types[3]
        group_order_type = groups_types[4]

    # --- root query fields ---------------------------------------------------
    def resolve_list(
        self: Any,
        info: strawberry.Info,
        where: Any = None,
        order_by: Any = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> Any:
        qs = apply_ordering(filtered(info, where), order_by)
        return paginate(qs, limit, offset)

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
        qs = get_queryset(info).filter(**{id_column: lookup})
        # Lean on strawberry-django's optimizer for the single row's nested
        # selections too. ``.first()`` evaluates eagerly, so — unlike the list
        # / ``nodes`` resolvers, whose lazy queryset the optimizer extension
        # auto-optimizes — by_pk must compose ``optimize()`` itself or its
        # relations N+1. ``optimize`` is a standalone primitive (it applies the
        # same select_related / prefetch / ``.only()`` hints with or without
        # the extension installed): it composes the wheel, never reinvents it.
        return optimize(qs, info).first()

    resolve_by_pk.__annotations__ = {
        "self": Any,
        "info": strawberry.Info,
        "id": str,
        "return": node | None,
    }

    list_root = res
    aggregate_root = f"{res}_aggregate"
    detail_root = f"{res}_by_pk"
    groups_root = f"{res}_groups" if groups_field is not None else None

    query_fields = {
        list_root: strawberry.field(resolver=resolve_list, name=list_root),
        aggregate_root: strawberry.field(
            resolver=resolve_aggregate, name=aggregate_root
        ),
        detail_root: strawberry.field(
            resolver=resolve_by_pk, name=detail_root
        ),
    }
    if groups_field is not None and groups_root is not None:
        query_fields[groups_root] = groups_field
    query = strawberry.type(type(f"{res}__query", (), query_fields))

    # --- root mutation fields ------------------------------------------------
    mutation_fields: dict[str, Any] = {}
    insert_one_root = f"insert_{res}_one" if "insert" in operations else None
    update_by_pk_root = (
        f"update_{res}_by_pk" if "update" in operations else None
    )
    delete_by_pk_root = (
        f"delete_{res}_by_pk" if "delete" in operations else None
    )
    if "insert" in operations:
        assert insert_input is not None
        assert insert_one_root is not None

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
        mutation_fields[insert_one_root] = strawberry.mutation(
            resolver=resolve_insert,
            name=insert_one_root,
        )
    if "update" in operations:
        assert pk_columns_input is not None
        assert set_input is not None
        assert update_by_pk_root is not None

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
        mutation_fields[update_by_pk_root] = strawberry.mutation(
            resolver=resolve_update,
            name=update_by_pk_root,
        )
    if "delete" in operations:
        assert delete_by_pk_root is not None

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
        mutation_fields[delete_by_pk_root] = strawberry.mutation(
            resolver=resolve_delete,
            name=delete_by_pk_root,
        )

    # A read-only resource (no enabled operations) yields an empty mutation
    # holder; it merges to nothing, so a consumer may register it uniformly.
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
        name=res,
        node_type=node,
        filter_type=bool_exp,
        order_by_type=order_by_input,
        insert_input_type=insert_input,
        set_input_type=set_input,
        pk_columns_input_type=pk_columns_input,
        aggregate_container_type=container,
        aggregate_type=aggregate_type,
        group_type=group_type,
        group_key_type=group_key_type,
        group_by_spec_type=group_by_spec_type,
        group_order_type=group_order_type,
        having_type=having_type,
        list_root=list_root,
        aggregate_root=aggregate_root,
        detail_root=detail_root,
        groups_root=groups_root,
        insert_one_root=insert_one_root,
        update_by_pk_root=update_by_pk_root,
        delete_by_pk_root=delete_by_pk_root,
        enabled_operations=operations,
        insertable_fields=(
            tuple(field.name for field in insert_fields) if insert else ()
        ),
        updatable_fields=(
            tuple(field.name for field in set_fields) if update else ()
        ),
    )
