"""Django settings for the test suite *and* the mypy ``django-stubs`` plugin.

The settings live in a real module (not only inlined in ``conftest.py``) so
``mypy_django_plugin`` can read them statically to resolve ORM/model field
types. The harness still configures Django programmatically — ``conftest``'s
``pytest_configure`` calls ``settings.configure()`` from these same constants,
so there is one source of truth and no ``DJANGO_SETTINGS_MODULE`` env var is
required to run the suite.
"""

from __future__ import annotations

DEBUG = True

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    },
}

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "tests.apps.TestsConfig",
]

USE_TZ = True
TIME_ZONE = "UTC"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
