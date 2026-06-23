"""strawberry-django-hasura — Django models as Hasura-dialect GraphQL.

Composes ``strawberry-django`` (types, the ORM seam) and
``strawberry-django-aggregates`` (the native ``<Model>Aggregate`` type +
``compute_aggregation``) to emit the GraphQL contract the stock
``@refinedev/hasura`` refine data provider speaks — unpatched.
``CONTRACT.md`` holds the exact target SDL.

Ownership rule (see ``AGENTS.md``): the ``strawberry`` /
``strawberry-django`` / ``strawberry-django-aggregates`` libraries are
*composed, never modified*. The Hasura ``aggregate`` is the library's own
``<Model>Aggregate`` type — there is no reshape layer (contrast the nestjs
path's ~300-LOC ``aggregation.py``).
"""

from __future__ import annotations

from .aggregation import build_aggregate_type, make_aggregate_resolver
from .connection import make_aggregate_container, paginate
from .filtering import comparison_to_q, where_to_q
from .mutations import input_to_dict
from .naming import SnakeNameConverter, hasura_config
from .ordering import OrderBy, apply_ordering, order_clauses

__all__ = [
    "OrderBy",
    "SnakeNameConverter",
    "apply_ordering",
    "build_aggregate_type",
    "comparison_to_q",
    "hasura_config",
    "input_to_dict",
    "make_aggregate_container",
    "make_aggregate_resolver",
    "order_clauses",
    "paginate",
    "where_to_q",
]
