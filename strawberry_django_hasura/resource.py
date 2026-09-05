"""One-call ``hasura_resource(...)`` — the full Hasura surface for a model.

The six surfaces (``comparisons`` / ``filtering`` / ``ordering`` /
``connection`` / ``mutations`` / ``aggregation`` / ``grouping``) are
model-independent primitives. Composing them into a working resource is
otherwise hand-wiring per model: declare the ``<res>_bool_exp`` /
``<res>_order_by`` / ``<res>_insert_input`` / ``<res>_set_input`` /
``<res>_pk_columns_input``
inputs, the ``<res>`` / ``<res>_aggregate`` / ``<res>_by_pk`` query fields, the
optional ``<res>_groups`` / ``<res>_groups_count`` grouped fields, the
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
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Any, Protocol, cast

import strawberry
from django.core.exceptions import FieldDoesNotExist
from django.db.models import Field, Model, QuerySet
from strawberry import UNSET
from strawberry_django.fields.types import (
    field_type_map,
    input_field_type_map,
)
from strawberry_django.resolvers import django_resolver
from strawberry_django_aggregates import AggregateBuilder, group_by_alias

from .aggregation import make_aggregate_resolver
from .comparisons import IDComparison
from .connection import (
    _optimize_queryset,
    capped_limit,
    make_aggregate_container,
    paginate,
)
from .filtering import _filter_lookups, filter_queryset, where_to_q
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
from .ordering import apply_ordering, validate_sortable


class FilterablePathError(ValueError):
    """A declared filterable path is not scalar/to-one safe.

    Filter paths are schema declarations, so an invalid path is a build-time
    configuration error rather than a request-time ORM failure.  The named
    exception lets schema assemblers distinguish that validation failure from
    an unsupported scalar comparison or another builder error.
    """


class WriteBackend(Protocol):
    """The caller-supplied authorized-write seam for the mutation surface.

    Persistence (and its authorization — REBAC gates, ``full_clean``, relation
    coercion) belongs to the model / the consuming app, not this library. Each
    Hasura write dispatches to one method here with the input already reduced
    to plain (possibly nested) dicts — never a strawberry input instance. The
    sqid⇄pk decode at the write boundary stays this backend's concern; the toy
    demo wraps the bare ORM, a real consumer wraps its CRUD machinery.
    ``delete`` returns the deleted instance (or ``None``) so the Hasura
    ``delete_<res>_by_pk`` response can resolve the removed row.
    """

    def create(self, info: strawberry.Info, data: dict[str, Any]) -> Any: ...

    def update(
        self, info: strawberry.Info, pk: str, data: dict[str, Any]
    ) -> Any: ...

    def delete(self, info: strawberry.Info, pk: str) -> Any | None: ...


@dataclass(frozen=True)
class NestedInsert:
    """A declared to-many child relation exposed as a Hasura nested insert.

    Hasura writes a parent and its array-relationship children in one
    ``insert_<res>_one(object: {..., <relation>: {data: [<child>...]}})``
    envelope, atomically. This declares one such relation for
    :func:`hasura_resource`:

    - ``relation`` names the parent's reverse-FK to-many relation — the
      ``related_name`` (``"lines"``) or, without one, either the default
      accessor (``"line_set"``) or the related query name (``"line"``); it is
      also the wire name of the parent input's envelope field. The child's
      foreign key back to the parent is derived from it and **excluded** from
      the child row input (the nesting supplies it); listing that column in
      ``insertable`` fails fast.
    - ``model`` is the child Django model — it must be the relation's target
      (checked at build). Its editable columns shape the child row input
      (``insertable`` overrides the auto allowlist, mirroring the top-level
      ``insertable`` knob).
    - ``public_id_columns`` marks child columns whose operands are public ids
      (typed ``ID`` in the input). Decoding them stays the write backend's
      concern, exactly like the parent's write path.
    - ``id_column`` names the child's public id column so it is excluded from
      the writable columns (server-owned), mirroring the top-level
      ``id_column`` (default ``"pk"`` — a raw-pk child).
    - ``name`` overrides the child input type stem (default
      ``f"{res}_{relation}"``).

    The generated child row input carries an **optional public ``id``** and
    every column is optional, so the one input drives both the nested insert
    (``id`` omitted → a new child) and a consumer's authored upsert/diff
    ``_save`` operation (``id`` present → an existing child). Persistence stays
    the caller's ``write_backend`` concern: ``input_to_dict`` reduces the
    ``{data: [...]}`` envelope to plain nested dicts and hands them to
    ``create`` — the backend writes parent + children in one transaction and
    rolls back on a child failure.
    """

    relation: str
    model: type[Model]
    name: str | None = None
    insertable: Sequence[str] | None = None
    public_id_columns: Sequence[str] | None = None
    id_column: str = "pk"

    def __post_init__(self) -> None:
        # Freeze the sequence knobs to tuples so the frozen spec stays
        # hashable however the caller spelled them.
        for knob in ("insertable", "public_id_columns"):
            value = getattr(self, knob)
            if value is not None:
                object.__setattr__(self, knob, tuple(value))


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
    the count-only ``<Node>Aggregate`` on the row-source path). Groupable
    resources expose both ``groups_root`` and the exact ``groups_count_root``.
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
    nested_inserts: tuple[NestedInsert, ...] = ()
    nested_input_types: Mapping[str, type] = dataclass_field(
        default_factory=dict
    )
    nested_arr_input_types: Mapping[str, type] = dataclass_field(
        default_factory=dict
    )
    # Appended for positional-constructor compatibility with <= 0.5.x.
    groups_count_root: str | None = None


def _column_python_type(field: Any, *, for_input: bool = False) -> Any:
    """The python type a Django column carries — asked of strawberry-django.

    Defers to the owner's scalar maps instead of re-listing them. Writable
    inputs use ``input_field_type_map`` overrides (e.g. ``Upload`` for a file)
    over ``field_type_map``. To-one relations carry their target key's scalar.
    The maps are keyed by exact field class; walk the MRO so a subclass
    inherits its base mapping (``EmailField`` → ``CharField`` → ``str``). A
    field type the owner does not map raises rather than degrading to ``str``
    (the library's fail-fast stance — see ``filtering.comparison_to_q``).
    """
    if getattr(field, "many_to_one", False) or getattr(
        field, "one_to_one", False
    ):
        return _column_python_type(field.target_field, for_input=for_input)
    type_map = (
        field_type_map | input_field_type_map if for_input else field_type_map
    )
    for klass in type(field).__mro__:
        if klass in type_map:
            return type_map[klass]
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


def _filterable_path_field(model: type[Model], path: str) -> Any:
    """Resolve a filterable declaration to its comparison Django field.

    A direct relation remains the adapter's established related-key
    comparison (``author: ID_comparison_exp`` or the related key's natural
    scalar). This includes a direct to-many field, whose exact lookup has
    Django's membership semantics. A nested declaration preserves that model
    under the exact Django lookup path (``book__author:
    ID_comparison_exp`` for an auto pk). Every segment of a multi-segment path
    must therefore avoid a to-many crossing, which would row-multiply.

    Django forbids ``__`` in model field names, so preserving the complete
    declared path as the GraphQL input field is deterministic and cannot
    collide with a direct field or another valid path. No parallel nested
    bool-exp type hierarchy is needed.
    """
    segments = path.split("__")
    if not path or any(not segment for segment in segments):
        raise FilterablePathError(
            f"filterable path {path!r} on {model.__name__} is malformed; "
            "use non-empty Django field names separated by '__'"
        )

    current_model = model
    for index, segment in enumerate(segments):
        terminal = index == len(segments) - 1
        try:
            field = current_model._meta.get_field(segment)
        except FieldDoesNotExist as exc:
            if terminal:
                raise FilterablePathError(
                    f"filterable path {path!r} on {model.__name__} "
                    f"terminates at {segment!r} on "
                    f"{current_model.__name__}, which is neither a "
                    "relation nor a scalar field"
                ) from exc
            raise FilterablePathError(
                f"filterable path {path!r} on {model.__name__} has no "
                f"segment {segment!r} on {current_model.__name__}; every "
                "non-terminal segment must be a to-one relation"
            ) from exc

        to_many = getattr(field, "one_to_many", False) or getattr(
            field, "many_to_many", False
        )
        if to_many and len(segments) > 1:
            raise FilterablePathError(
                f"filterable path {path!r} on {model.__name__} crosses "
                f"to-many relation {segment!r} on "
                f"{current_model.__name__}; only to-one relation paths "
                "are filterable"
            )

        if terminal:
            if to_many:
                # Before nested paths existed, a direct field was resolved by
                # ``model._meta.get_field`` and its exact lookup was passed to
                # Django verbatim. Preserve that membership-filter surface,
                # deriving the comparison from the related lookup target.
                target_field = getattr(field, "target_field", None)
                if target_field is not None:
                    return target_field
                related_model = getattr(field, "related_model", None)
                if related_model is not None:
                    return related_model._meta.pk
            if isinstance(field, Field):
                return field
            # A reverse one-to-one relation has no local target_field, but an
            # exact relation lookup still compares against the related pk.
            if getattr(field, "one_to_one", False):
                related_model = getattr(field, "related_model", None)
                if related_model is not None:
                    return related_model._meta.pk
            raise FilterablePathError(
                f"filterable path {path!r} on {model.__name__} terminates "
                f"at {segment!r} on {current_model.__name__}, which is "
                "neither a to-one relation nor a scalar field"
            )

        related_model = getattr(field, "related_model", None)
        is_to_one = getattr(field, "many_to_one", False) or getattr(
            field, "one_to_one", False
        )
        if not is_to_one or related_model is None:
            raise FilterablePathError(
                f"filterable path {path!r} on {model.__name__} cannot "
                f"traverse segment {segment!r} on "
                f"{current_model.__name__}; it is not a to-one relation"
            )
        current_model = related_model

    raise AssertionError("a non-empty filterable path always has a terminal")


def _writable_fields(
    model: type[Model],
    id_column: str,
    writable: Sequence[str] | None = None,
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
    # A reverse accessor (ManyToOneRel / ManyToManyRel / OneToOneRel) is not a
    # Field: the OTHER model owns that relation, so it is never client-settable
    # here — including a reverse many-to-many, which would otherwise slip
    # through the forward-m2m allowance below.
    if not isinstance(field, Field):
        return "it is a reverse relation accessor, not a column"
    if getattr(field, "primary_key", False):
        return "it is the primary key"
    if field.name == id_column:
        return "it is the public id column"
    if not getattr(field, "editable", False):
        return "it is not editable"
    if getattr(field, "many_to_many", False):
        return None
    if not getattr(field, "concrete", False):
        return "it is not a concrete column"
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
            else _column_python_type(field.target_field, for_input=True)
        )
        return types.GenericAlias(list, (item_type,))
    return (
        strawberry.ID
        if public_id
        else _column_python_type(field, for_input=True)
    )


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


def _optional_on_insert(column: Any) -> bool:
    """Return whether a writable column may be omitted from an insert input."""

    return (
        column.has_default()
        or column.has_db_default()
        or getattr(column, "null", False)
        or getattr(column, "blank", False)
    )


def _insert_input_type(
    name: str,
    fields: list[Any],
    public_id_fields: frozenset[str],
    module: types.ModuleType,
    *,
    with_public_id: bool = False,
    extra: Mapping[str, type] | None = None,
) -> type:
    """Build a ``<name>_insert_input`` from a model's writable fields.

    Shared by the parent insert input and each nested child row input. A
    concrete column with a Python or database default, null, or blank is
    optional.
    ``with_public_id`` prepends an optional public ``id`` (the upsert key) and
    makes every column optional, so the one child input serves both the nested
    insert (``id`` omitted) and a consumer's upsert diff (``id`` present).
    ``extra`` adds the already-built nested array-relation envelope fields
    (always optional) to the parent input.
    """

    annotations: dict[str, Any] = {}
    defaults: dict[str, Any] = {}
    if with_public_id:
        annotations["id"] = str | None
        defaults["id"] = UNSET
    for column in fields:
        if with_public_id and column.name == "id":
            raise TypeError(
                f"column 'id' collides with the public upsert key injected "
                f"into {name}; exclude it (id_column / insertable) or rename "
                "the model column"
            )
        python_type = _writable_python_type(
            column,
            public_id=column.name in public_id_fields,
        )
        optional = with_public_id or _optional_on_insert(column)
        annotations[column.name] = (
            python_type | None if optional else python_type
        )
        if optional:
            defaults[column.name] = UNSET
    for attr, attr_type in (extra or {}).items():
        annotations[attr] = attr_type | None
        defaults[attr] = UNSET
    return _input_type(name, annotations, module=module, defaults=defaults)


def _child_relation(model: type[Model], relation: str) -> Any:
    """Resolve a declared nested relation to the parent's reverse-FK rel.

    Accepts the ``related_name`` / related query name (what ``get_field``
    resolves) or the default ``<child>_set`` accessor, and fails fast — with
    the relation named — on anything that is not a reverse to-many foreign
    key (a forward field, a reverse one-to-one, a reverse many-to-many).
    """
    reverse: Any
    try:
        reverse = model._meta.get_field(relation)
    except FieldDoesNotExist:
        reverse = next(
            (
                rel
                for rel in model._meta.related_objects
                if rel.get_accessor_name() == relation
            ),
            None,
        )
        if reverse is None:
            raise FieldDoesNotExist(
                f"{model.__name__} has no relation {relation!r} "
                "(no field, related name, or reverse accessor matches)"
            ) from None
    if not getattr(reverse, "one_to_many", False):
        raise TypeError(
            f"NestedInsert relation {relation!r} on {model.__name__} is "
            f"{type(reverse).__name__}, not a reverse to-many foreign key; "
            "a nested object insert needs an array relationship"
        )
    return reverse


def hasura_resource(  # noqa: PLR0913 — declarative builder: one knob per facet
    node: type,
    *,
    model: type[Model],
    name: str | None = None,
    filterable: list[str],
    sortable: list[str],
    aggregatable: list[str],
    aggregate_name: str | None = None,
    groupable: list[str] | None = None,
    json_paths: Mapping[str, str] | None = None,
    group_key_encoders: Mapping[str, Callable[[Any], Any]] | None = None,
    filter_lookups: Mapping[str, tuple[str, bool]] | None = None,
    max_groups: int | None = None,
    max_rows: int | None = None,
    writable: list[str] | None = None,
    insertable: list[str] | None = None,
    updatable: list[str] | None = None,
    nested: list[NestedInsert] | None = None,
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
    ``<Model>Aggregate``. A filterable entry may be a ``__`` path through
    to-one relations; its terminal scalar or relation uses the same comparison
    input as a direct field and the complete path is its bool-exp field name.
    ``groupable`` enables the optional NDC-shaped
    ``<res>_groups`` row root and exact ``<res>_groups_count`` companion;
    ``max_groups`` caps only the row root's offset page (a high-cardinality
    dimension would otherwise pull every group — default ``None`` is uncapped;
    pass ``order_by`` for stable pages).
    ``max_rows`` caps list and aggregate-node pages while keeping aggregate
    math unpaged. ``aggregate_name`` overrides the resource-stem prefix used
    for native aggregate and grouping types; choose a unique prefix in the
    consuming schema.
    The json_paths mapping declares typed JSON paths for grouped and
    ungrouped measures and dimensions. The group_key_encoders mapping
    supplies output identity codecs for declared group paths, preserving
    nulls and the generated scalar. The filter_lookups mapping extends
    portable operators for this resource. All mappings are copied at
    construction. ``writable``
    mirrors Hasura field
    permissions for insert / ``_set`` inputs (default: editable concrete model
    columns plus editable many-to-many relation arrays). ``insertable`` and
    ``updatable`` override that shared allowlist for insert and update
    separately. ``nested`` declares to-many child relations exposed as Hasura
    array-relationship inserts (:class:`NestedInsert`); it requires
    ``insert=True``. ``insert`` / ``update`` / ``delete``
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
    snake_case. Output node fields remain caller-owned: explicitly name them
    or use ``hasura_config()`` when snake_case is required on those fields.
    """
    res = name or model.__name__.lower()
    capped_limit(None, max_rows)
    capped_limit(None, max_groups)
    active_json_paths = dict(json_paths or {})
    active_encoders = dict(group_key_encoders or {})
    active_lookups = _filter_lookups(filter_lookups)
    for path, encoder in active_encoders.items():
        if path not in (groupable or []) or not callable(encoder):
            raise ValueError(
                f"Group-key encoder {path!r} must name a declared groupable "
                "path and be callable"
            )
    aliases: dict[str, str] = {}
    for path in {*aggregatable, *(groupable or []), *active_json_paths}:
        alias = group_by_alias(path, None)
        if alias in aliases and aliases[alias] != path:
            raise ValueError(
                f"Aggregate paths {aliases[alias]!r} and {path!r} "
                f"share alias {alias!r}"
            )
        aliases[alias] = path
    public_id_fields = frozenset(field_id_decode or {})
    operations = _enabled_operations(
        insert=insert,
        update=update,
        delete=delete,
    )
    # Resource-scoped names isolate independently configured aggregate types
    # even when two resources share a node. A legacy prefix is an explicit
    # caller choice and must be unique within its consuming schema.
    aggregate_prefix = aggregate_name or res
    module = _host_module(res)
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
                    _filterable_path_field(model, col),
                    public_id=col in public_id_fields,
                )
            )
            for col in filterable
        },
        module,
    )
    validate_sortable(model, sortable, id_column=id_column)
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
    # --- nested to-many child inserts (Hasura array-relationship inserts) ----
    # Each declared relation contributes a child row input + its {data: [...]}
    # envelope, and an optional field on the parent insert input. The child's
    # FK back to the parent is excluded (the nesting supplies it); the child
    # input carries an optional public ``id`` so it also serves the consumer's
    # upsert diff. Persistence stays the ``write_backend``'s concern. Every
    # spec is validated here — misdeclared relations fail at build, not at the
    # first request.
    nested_specs = tuple(nested or ())
    if nested_specs and not insert:
        raise TypeError(
            "nested= declares insert-only input types; it requires insert=True"
        )
    nested_input_types: dict[str, type] = {}
    nested_arr_input_types: dict[str, type] = {}
    for spec in nested_specs:
        child_stem = spec.name or f"{res}_{spec.relation}"
        reverse = _child_relation(model, spec.relation)
        if reverse.related_model is not spec.model:
            raise TypeError(
                f"NestedInsert(relation={spec.relation!r}) targets "
                f"{reverse.related_model.__name__}, but model= is "
                f"{spec.model.__name__}"
            )
        back_fk = reverse.field.name
        if spec.insertable is not None and back_fk in spec.insertable:
            raise TypeError(
                f"column {back_fk!r} is the child's foreign key back to the "
                "parent; the nesting supplies it — remove it from insertable"
            )
        child_fields = [
            column
            for column in _writable_fields(
                spec.model, spec.id_column, spec.insertable
            )
            if column.name != back_fk
        ]
        child_input = _insert_input_type(
            f"{child_stem}_insert_input",
            child_fields,
            frozenset(spec.public_id_columns or ()),
            module,
            with_public_id=True,
        )
        arr_input = _input_type(
            f"{child_stem}_arr_rel_insert_input",
            {"data": types.GenericAlias(list, (child_input,))},
            module=module,
        )
        nested_input_types[spec.relation] = child_input
        nested_arr_input_types[spec.relation] = arr_input
    nested_types = [
        *nested_input_types.values(),
        *nested_arr_input_types.values(),
    ]

    # insert: required only when the model field has no default and is not
    # nullable. Columns with Django defaults are omitted from the resolver
    # input and let the model apply its default; the GraphQL SDL does not
    # mirror Python default values (especially mutable / JSON defaults). A
    # declared to-many relation adds an optional nested-insert envelope field.
    insert_input: type | None = None
    if insert:
        insert_input = _insert_input_type(
            f"{res}_insert_input",
            insert_fields,
            public_id_fields,
            module,
            extra=nested_arr_input_types,
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
        return filter_queryset(
            source(info),
            where_to_q(
                where,
                id_column=id_column,
                id_decode=id_decode,
                field_decoders=field_id_decode,
                lookups=active_lookups,
            ),
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
        json_paths=active_json_paths,
    )
    agg_built = agg_builder.build()
    aggregate_type = cast("type", agg_built.aggregate_type)
    _pin_snake_wire_names(aggregate_type, recursive=True)
    aggregate_resolver = make_aggregate_resolver(
        aggregate_type, json_paths=active_json_paths
    )
    container = make_aggregate_container(
        f"{res}_aggregate",
        node,
        aggregate_type,
        filtered_queryset=filtered_aggregate,
        filtered_nodes_queryset=filtered,
        aggregate_resolver=aggregate_resolver,
        max_rows=max_rows,
    )
    groups_field: Any = None
    groups_count_field: Any = None
    groups_types: list[type] = []
    group_type: type | None = None
    group_key_type: type | None = None
    group_by_spec_type: type | None = None
    group_order_type: type | None = None
    having_type: type | None = None
    if groupable:
        groups_field, groups_count_field, groups_types = make_groups_field(
            builder=agg_builder,
            built=agg_built,
            resource_name=res,
            filter_type=bool_exp,
            get_queryset=aggregate_get_queryset,
            id_decode=id_decode,
            id_column=id_column,
            field_decoders=field_id_decode,
            max_groups=max_groups,
            group_key_encoders=active_encoders,
            filter_lookups=active_lookups,
        )
        # Pin snake_case on the generated group types — the query walk reaches
        # the group container + key (a return type) but not the ``having`` /
        # ``order_by`` INPUT types (e.g. ``count_gt`` would camelCase).
        for grouped in groups_types:
            _pin_snake_wire_names(grouped, recursive=True)
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
        qs = apply_ordering(
            filtered(info, where), order_by, id_column=id_column
        )
        return _optimize_queryset(
            paginate(qs, limit, offset, maximum=max_rows), info
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
        lookup = id_decode(id) if id_decode is not None else id
        qs = get_queryset(info).filter(**{id_column: lookup})
        # All row roots compose the public optimizer before evaluation.
        # ``django_resolver`` keeps that evaluation safe under both GraphQL
        # executors; the callback remains the root's row-scope owner.
        return _optimize_queryset(qs, info).first()

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
    groups_count_root = (
        f"{res}_groups_count" if groups_count_field is not None else None
    )

    query_fields = {
        list_root: strawberry.field(
            resolver=django_resolver(resolve_list),
            name=list_root,
        ),
        aggregate_root: strawberry.field(
            resolver=resolve_aggregate, name=aggregate_root
        ),
        detail_root: strawberry.field(
            resolver=django_resolver(resolve_by_pk), name=detail_root
        ),
    }
    if groups_field is not None and groups_root is not None:
        query_fields[groups_root] = groups_field
    if groups_count_field is not None and groups_count_root is not None:
        query_fields[groups_count_root] = groups_count_field
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
            data = input_to_dict(object)
            # Hasura semantics: an explicit ``<relation>: null`` envelope
            # means "no children", not a null column — the backend must see
            # the key absent, same as an omitted envelope.
            for relation in nested_arr_input_types:
                if data.get(relation) is None:
                    data.pop(relation, None)
            return write_backend.create(info, data)

        resolve_insert.__annotations__ = {
            "self": Any,
            "info": strawberry.Info,
            "object": insert_input,
            "return": node,
        }
        mutation_fields[insert_one_root] = strawberry.mutation(
            resolver=django_resolver(resolve_insert),
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
            resolver=django_resolver(resolve_update),
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
            resolver=django_resolver(resolve_delete),
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
                *nested_types,
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
        groups_count_root=groups_count_root,
        insert_one_root=insert_one_root,
        update_by_pk_root=update_by_pk_root,
        delete_by_pk_root=delete_by_pk_root,
        enabled_operations=operations,
        insertable_fields=(
            tuple(column.name for column in insert_fields) if insert else ()
        ),
        updatable_fields=(
            tuple(column.name for column in set_fields) if update else ()
        ),
        nested_inserts=nested_specs,
        nested_input_types=nested_input_types,
        nested_arr_input_types=nested_arr_input_types,
    )
