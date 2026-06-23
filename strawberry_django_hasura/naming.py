"""Hasura snake_case wire naming — a ``StrawberryConfig``, never a patch.

Hasura-default keeps snake_case on the wire (column names, the ``where`` /
``order_by`` / ``pk_columns`` / ``_set`` arguments). Strawberry camelCases
by default (``word_count`` → ``wordCount``, ``pk_columns`` → ``pkColumns``),
which would not match what the stock ``@refinedev/hasura`` provider sends
with ``namingConvention: "hasura-default"``. Installing this converter on the
schema config keeps every Python snake_case identifier verbatim on the wire.

This is a config flag (``strawberry.schema.config.StrawberryConfig``), not a
fork of the provider or of strawberry — the consumer passes it when building
the schema (see the quickstart in ``README.md``)::

    schema = strawberry.Schema(query=Query, config=hasura_config())
"""

from __future__ import annotations

from typing import Any

from strawberry.schema.config import StrawberryConfig
from strawberry.schema.name_converter import NameConverter


class SnakeNameConverter(NameConverter):
    """Keep Python snake_case names verbatim on the wire (Hasura convention).

    An explicit ``strawberry.field(name=...)`` (e.g. the ``_eq`` operators)
    still wins — only the default camelCasing of a python identifier is
    suppressed.
    """

    def get_graphql_name(self, obj: Any) -> str:
        graphql_name = getattr(obj, "graphql_name", None)
        if graphql_name:
            return str(graphql_name)
        return str(obj.python_name)


def hasura_config() -> StrawberryConfig:
    """A ``StrawberryConfig`` with the snake_case name converter installed."""
    return StrawberryConfig(name_converter=SnakeNameConverter())
