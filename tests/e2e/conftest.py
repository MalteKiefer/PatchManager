import pytest
from django.db import connection


@pytest.fixture(scope="session")
def django_db_setup(django_db_setup, django_db_blocker):
    """Live server tests flush tables with TRUNCATE between tests; allow it for the audit table.

    Only the throwaway test database is affected. The unit tests prove the trigger.
    """
    with django_db_blocker.unblock(), connection.cursor() as cursor:
        cursor.execute("DROP TRIGGER IF EXISTS audit_no_truncate ON audit_auditevent")


@pytest.fixture(autouse=True)
def _system_roles(transactional_db):
    """Live server tests flush tables, including rows created by data migrations."""
    from importlib import import_module

    from django.apps import apps

    import_module("patchmanager.accounts.migrations.0002_system_roles").create(apps, None)
