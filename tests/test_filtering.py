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

import pytest

from strawberry_django_hasura.comparisons import (
    BooleanComparison,
    DateTimeComparison,
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


def test_json_contains_operator_builds_the_expected_lookup():
    q = comparison_to_q("metadata", JSONComparison(contains={"kind": "note"}))
    assert ("metadata__contains", {"kind": "note"}) in q.children
