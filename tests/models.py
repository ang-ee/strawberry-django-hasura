"""The toy ``Note`` model for the test suite.

Django auto-imports ``<app>.models`` at setup, registering ``NoteModel`` so
pytest-django's ``db`` fixture builds its table via run-syncdb. The Hasura-
shaped schema is built in ``tests.demo_schema`` (imported by the fixtures
*after* ``django.setup``) — ``AggregateBuilder`` introspects the model's
relation tree, which is only ready once the app registry is fully populated,
so schema construction cannot run during ``models`` import. This split also
mirrors a real consumer: the model lives in ``app/models.py``, the schema
imports it.
"""

from __future__ import annotations

from django.db import models


class NoteModel(models.Model):
    title = models.CharField(max_length=200)
    word_count = models.IntegerField(default=0)
    is_starred = models.BooleanField(default=False)
    status = models.CharField(max_length=20, default="draft")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "tests"


__all__ = ["NoteModel"]
