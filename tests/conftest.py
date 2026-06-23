"""Pytest configuration for strawberry-django-hasura tests.

Provides a minimal in-memory SQLite Django setup so tests can build
strawberry schemas and run the adapter's CRUD + free-aggregate surfaces
without booting a full project. Mirrors the sibling
``strawberry-django-aggregates`` test harness: settings are configured
programmatically here (from the shared ``tests.settings`` constants, which
the mypy ``django-stubs`` plugin also reads), and the toy ``Note`` model
(``tests.models``) registers against the ``tests`` app, whose table
pytest-django's ``db`` fixture creates via run-syncdb.
"""

from __future__ import annotations

import django
import pytest
from django.conf import settings

from tests import settings as test_settings


def pytest_configure() -> None:
    if settings.configured:
        return
    settings.configure(
        DEBUG=test_settings.DEBUG,
        DATABASES=test_settings.DATABASES,
        INSTALLED_APPS=test_settings.INSTALLED_APPS,
        USE_TZ=test_settings.USE_TZ,
        TIME_ZONE=test_settings.TIME_ZONE,
        DEFAULT_AUTO_FIELD=test_settings.DEFAULT_AUTO_FIELD,
    )
    django.setup()


@pytest.fixture
def schema():
    """The toy ``Note`` Hasura schema (imported after ``django.setup``)."""
    from tests.demo_schema import schema as note_schema

    return note_schema


@pytest.fixture
def seeded_notes(db):
    """Seed the three baseline notes and return the model class.

    Alpha (published, 10), Bravo (draft, 30), Cee (published, 20).
    """
    from tests.demo_schema import seed
    from tests.models import NoteModel

    seed()
    return NoteModel
