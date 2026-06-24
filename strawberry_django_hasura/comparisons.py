"""Hasura ``<scalar>_comparison_exp`` inputs (model-independent).

Each comparison carries the operator set Hasura exposes for a scalar type. The
operator *field names* (``_eq``/``_neq``/``_ilike``/…) are the wire contract
the stock ``@refinedev/hasura`` provider sends; the input *type names*
(``String_comparison_exp`` etc.) are the real Hasura names — refine references
a ``<field>_comparison_exp`` per column in its generated
``<resource>_bool_exp``, so keeping the canonical names makes the SDL read like
a Hasura schema. One translator (``filtering.comparison_to_q``) reads any of
these via ``getattr``, so a scalar that omits an operator simply lacks the
attribute.

The operator names mirror refine's ``hasuraFilterOperatorMappings``:
``eq→_eq``, ``ne→_neq``, ``lt/gt/lte/gte``, ``in→_in``, ``nin→_nin``,
``contains→_ilike``, ``containss→_like``, ``null→_is_null``. Postgres-only
operators the provider can send for some refine ``CrudOperator``\\ s —
``_nilike``/``_nlike`` (``ncontains`` /``ncontainss``), ``_iregex``
(``startswith``/``endswith``), ``_similar`` (``startswiths``/``endswiths``) —
are included on ``String_comparison_exp`` so the SDL accepts them. The
portable ones are mapped in ``filtering._LOOKUPS``; the Postgres-only
regex/similar lookups stay project-supplied, and sending one a backend has not
registered raises rather than being silently dropped (see
``filtering.comparison_to_q``).
"""

from __future__ import annotations

import datetime

import strawberry
from strawberry import UNSET
from strawberry.scalars import JSON


@strawberry.input(name="String_comparison_exp")
class StringComparison:
    eq: str | None = strawberry.field(name="_eq", default=UNSET)
    neq: str | None = strawberry.field(name="_neq", default=UNSET)
    gt: str | None = strawberry.field(name="_gt", default=UNSET)
    gte: str | None = strawberry.field(name="_gte", default=UNSET)
    lt: str | None = strawberry.field(name="_lt", default=UNSET)
    lte: str | None = strawberry.field(name="_lte", default=UNSET)
    in_: list[str] | None = strawberry.field(name="_in", default=UNSET)
    nin: list[str] | None = strawberry.field(name="_nin", default=UNSET)
    like: str | None = strawberry.field(name="_like", default=UNSET)
    nlike: str | None = strawberry.field(name="_nlike", default=UNSET)
    ilike: str | None = strawberry.field(name="_ilike", default=UNSET)
    nilike: str | None = strawberry.field(name="_nilike", default=UNSET)
    iregex: str | None = strawberry.field(name="_iregex", default=UNSET)
    similar: str | None = strawberry.field(name="_similar", default=UNSET)
    nsimilar: str | None = strawberry.field(name="_nsimilar", default=UNSET)
    is_null: bool | None = strawberry.field(name="_is_null", default=UNSET)


@strawberry.input(name="Int_comparison_exp")
class IntComparison:
    eq: int | None = strawberry.field(name="_eq", default=UNSET)
    neq: int | None = strawberry.field(name="_neq", default=UNSET)
    gt: int | None = strawberry.field(name="_gt", default=UNSET)
    gte: int | None = strawberry.field(name="_gte", default=UNSET)
    lt: int | None = strawberry.field(name="_lt", default=UNSET)
    lte: int | None = strawberry.field(name="_lte", default=UNSET)
    in_: list[int] | None = strawberry.field(name="_in", default=UNSET)
    nin: list[int] | None = strawberry.field(name="_nin", default=UNSET)
    is_null: bool | None = strawberry.field(name="_is_null", default=UNSET)


@strawberry.input(name="Float_comparison_exp")
class FloatComparison:
    eq: float | None = strawberry.field(name="_eq", default=UNSET)
    neq: float | None = strawberry.field(name="_neq", default=UNSET)
    gt: float | None = strawberry.field(name="_gt", default=UNSET)
    gte: float | None = strawberry.field(name="_gte", default=UNSET)
    lt: float | None = strawberry.field(name="_lt", default=UNSET)
    lte: float | None = strawberry.field(name="_lte", default=UNSET)
    in_: list[float] | None = strawberry.field(name="_in", default=UNSET)
    nin: list[float] | None = strawberry.field(name="_nin", default=UNSET)
    is_null: bool | None = strawberry.field(name="_is_null", default=UNSET)


@strawberry.input(name="Boolean_comparison_exp")
class BooleanComparison:
    eq: bool | None = strawberry.field(name="_eq", default=UNSET)
    neq: bool | None = strawberry.field(name="_neq", default=UNSET)
    is_null: bool | None = strawberry.field(name="_is_null", default=UNSET)


@strawberry.input(name="DateTime_comparison_exp")
class DateTimeComparison:
    eq: datetime.datetime | None = strawberry.field(name="_eq", default=UNSET)
    neq: datetime.datetime | None = strawberry.field(
        name="_neq", default=UNSET
    )
    gt: datetime.datetime | None = strawberry.field(name="_gt", default=UNSET)
    gte: datetime.datetime | None = strawberry.field(
        name="_gte", default=UNSET
    )
    lt: datetime.datetime | None = strawberry.field(name="_lt", default=UNSET)
    lte: datetime.datetime | None = strawberry.field(
        name="_lte", default=UNSET
    )
    in_: list[datetime.datetime] | None = strawberry.field(
        name="_in", default=UNSET
    )
    nin: list[datetime.datetime] | None = strawberry.field(
        name="_nin", default=UNSET
    )
    is_null: bool | None = strawberry.field(name="_is_null", default=UNSET)


@strawberry.input(name="JSON_comparison_exp")
class JSONComparison:
    eq: JSON | None = strawberry.field(name="_eq", default=UNSET)
    neq: JSON | None = strawberry.field(name="_neq", default=UNSET)
    contains: JSON | None = strawberry.field(name="_contains", default=UNSET)
    is_null: bool | None = strawberry.field(name="_is_null", default=UNSET)


@strawberry.input(name="ID_comparison_exp")
class IDComparison:
    """Comparison for the public ``id`` column.

    The pk surface is GraphQL ``String`` (see ``AGENTS.md`` — refine's
    ``idType`` declares the id variable type verbatim, never ``ID``), so the
    operand fields are typed ``str``. A consumer whose ``id`` is an opaque sqid
    decodes the value before the ORM lookup (see ``filtering``); for a raw-pk
    project the value passes straight through.
    """

    eq: str | None = strawberry.field(name="_eq", default=UNSET)
    neq: str | None = strawberry.field(name="_neq", default=UNSET)
    in_: list[str] | None = strawberry.field(name="_in", default=UNSET)
    nin: list[str] | None = strawberry.field(name="_nin", default=UNSET)
    is_null: bool | None = strawberry.field(name="_is_null", default=UNSET)
