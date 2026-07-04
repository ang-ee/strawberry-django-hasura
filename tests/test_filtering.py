"""Filtering-translator guards — the SDL operator vocabulary and the
``_LOOKUPS`` map must stay in agreement, and a set-but-unmapped operator must
fail loudly rather than silently widen a permission-naive read.

These pin the contract the two reviews surfaced: ``comparisons`` declares the
operator *fields* (the SDL) and ``filtering._LOOKUPS`` maps the *portable* ones
to Django lookups; the Postgres-only regex/similar operators are accepted in
the SDL but deliberately project-supplied (CLAUDE.md portability rule).
"""

from __future__ import annotations

import dataclasses
from decimal import Decimal

import pytest

from strawberry_django_hasura.comparisons import (
    BooleanComparison,
    DateTimeComparison,
    DecimalComparison,
    FloatComparison,
    IDComparison,
    IntComparison,
    JSONComparison,
    StringComparison,
)
from strawberry_django_hasura.filtering import _LOOKUPS, comparison_to_q

# Operators that are intentionally SDL-only: accepted on
# ``String_comparison_exp`` so the stock refine document validates, but absent
# from the portable default ``_LOOKUPS`` (a project registers them on a backend
# that supports them).
_SDL_ONLY = {"iregex", "similar", "nsimilar"}

_COMPARISONS = [
    StringComparison,
    IntComparison,
    FloatComparison,
    DecimalComparison,
    BooleanComparison,
    DateTimeComparison,
    IDComparison,
    JSONComparison,
]


@pytest.mark.parametrize("cls", _COMPARISONS)
def test_every_operator_field_is_mapped_or_documented(cls):
    """No silent drift: each comparison field is mapped in ``_LOOKUPS``, is the
    ``_is_null`` special case, or is a documented SDL-only operator."""
    for f in dataclasses.fields(cls):
        assert (
            f.name in _LOOKUPS or f.name == "is_null" or f.name in _SDL_ONLY
        ), f"{cls.__name__}.{f.name} is neither mapped nor documented SDL-only"


def test_postgres_only_operators_stay_out_of_the_default_lookups():
    """Portability rule (CLAUDE.md): the regex/similar lookups are not in the
    shared default map — they are project-supplied per backend."""
    assert not (_SDL_ONLY & _LOOKUPS.keys())


def test_set_but_unmapped_operator_raises_not_silently_drops():
    """A Postgres-only operator on a backend whose ``_LOOKUPS`` does not map it
    raises — it must not return an unfiltered ``Q()`` that widens the read."""
    with pytest.raises(ValueError):
        comparison_to_q("title", StringComparison(similar="Public%"))


def test_mapped_operator_builds_the_expected_lookup():
    q = comparison_to_q("title", StringComparison(ilike="a"))
    assert ("title__icontains", "a") in q.children


def test_ilike_accepts_refine_contains_wildcard_pattern():
    q = comparison_to_q("title", StringComparison(ilike="%a%"))
    assert ("title__icontains", "a") in q.children


def test_ilike_accepts_hasura_prefix_and_suffix_patterns():
    prefix = comparison_to_q("title", StringComparison(ilike="Al%"))
    suffix = comparison_to_q("title", StringComparison(ilike="%ha"))
    assert ("title__istartswith", "Al") in prefix.children
    assert ("title__iendswith", "ha") in suffix.children


def test_json_contains_operator_builds_the_expected_lookup():
    q = comparison_to_q("metadata", JSONComparison(contains={"kind": "note"}))
    assert ("metadata__contains", {"kind": "note"}) in q.children


def test_decimal_comparison_carries_the_exact_operand_a_float_would_lose():
    """The Decimal comparison hands the ORM the exact ``Decimal`` operand (the
    F1 fix). A ``FloatComparison`` would carry ``float(operand)``, which
    collapses a sub-ULP difference onto a *different* value, so the intended
    row would not match. Backend-independent: the translator is exact; only a
    float-affinity backend (SQLite) floors it, Postgres keeps it."""
    exact = Decimal("123456789012.100001")
    q = comparison_to_q("price", DecimalComparison(eq=exact))
    (child,) = q.children
    field, operand = child
    assert field == "price"
    assert operand == exact  # exact Decimal reaches the lookup, not a float
    assert isinstance(operand, Decimal)
    # What a FloatComparison would have carried instead: float() collapses
    # ...100001 onto ...100000 — a value that would match the wrong row.
    float_lossy = Decimal(str(float(exact)))
    assert float_lossy != exact
    assert float_lossy == Decimal(str(float(Decimal("123456789012.100000"))))


def test_decimal_in_operator_preserves_each_exact_operand():
    values = [Decimal("12345678.123456"), Decimal("0.000001")]
    (child,) = comparison_to_q("price", DecimalComparison(in_=values)).children
    field, operand = child
    assert field == "price__in"
    assert operand == values
    assert all(isinstance(v, Decimal) for v in operand)


@pytest.mark.django_db
def test_filter_by_exact_decimal_string_matches_through_the_schema(
    schema, seeded_notes
):
    """End to end: an exact high-precision operand sent as a string on the wire
    matches its row, and the node value comes back as that exact string — never
    a lossy Float. An off-by-last-digit operand matches nothing."""
    hit = schema.execute_sync(
        "query($w: notes_bool_exp){ notes(where: $w){ title price } }",
        variable_values={"w": {"price": {"_eq": "12345678.123456"}}},
    )
    assert hit.errors is None, hit.errors
    rows = hit.data["notes"]
    assert [row["title"] for row in rows] == ["Alpha"]
    # Exact string on the wire (strawberry Decimal scalar), not a float number.
    assert rows[0]["price"] == "12345678.123456"
    assert isinstance(rows[0]["price"], str)

    miss = schema.execute_sync(
        "query($w: notes_bool_exp){ notes(where: $w){ title } }",
        variable_values={"w": {"price": {"_eq": "12345678.123457"}}},
    )
    assert miss.errors is None, miss.errors
    assert miss.data["notes"] == []
