"""Row Level Security: the database hides foreign tenants even without application filters."""

import pytest
from django.db import connection, transaction
from django.db.utils import ProgrammingError

from patchmanager.tenancy import rls
from patchmanager.tenancy.models import Tenant

pytestmark = pytest.mark.django_db


def test_no_context_sees_no_tenants(make_tenant) -> None:
    make_tenant()
    rls.clear_context()
    assert Tenant.objects.count() == 0


def test_context_limits_rows_to_listed_tenants(make_tenant) -> None:
    a, b = make_tenant(), make_tenant()
    with rls.tenant_context([a.pk]):
        assert list(Tenant.objects.values_list("pk", flat=True)) == [a.pk]
        assert not Tenant.objects.filter(pk=b.pk).exists()


def test_all_tenants_flag(make_tenant) -> None:
    make_tenant(), make_tenant()
    with rls.system_context():
        assert Tenant.objects.count() == 2


def test_update_and_delete_of_foreign_rows_affect_nothing(make_tenant, system) -> None:
    a, b = make_tenant(), make_tenant(name="Original")
    with rls.tenant_context([a.pk]):
        assert Tenant.objects.filter(pk=b.pk).update(name="Hijacked") == 0
        assert Tenant.objects.filter(pk=b.pk).delete()[0] == 0
    with system():
        assert Tenant.objects.get(pk=b.pk).name == "Original"


def test_insert_outside_context_is_rejected(db) -> None:
    rls.clear_context()
    with pytest.raises(ProgrammingError, match="row-level security"), transaction.atomic():
        Tenant.objects.create(slug="sneaky", name="Sneaky")


def test_policy_binds_table_owner(db) -> None:
    """FORCE RLS: the connecting role owns the table and is still filtered."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT relforcerowsecurity, relrowsecurity FROM pg_class"
            " WHERE relname = 'tenancy_tenant'"
        )
        assert cursor.fetchone() == (True, True)
        cursor.execute("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
        assert cursor.fetchone() == (False, False)


def test_nested_context_restores_outer(make_tenant) -> None:
    a, b = make_tenant(), make_tenant()
    with rls.tenant_context([a.pk]):
        with rls.tenant_context([b.pk]):
            assert list(Tenant.objects.values_list("pk", flat=True)) == [b.pk]
        assert list(Tenant.objects.values_list("pk", flat=True)) == [a.pk]


def test_request_cannot_reach_foreign_tenant(client, make_user, make_tenant, grant) -> None:
    from tests.conftest import add_totp, login

    mine, other = make_tenant(), make_tenant()
    user = make_user()
    grant(user, "tenant_owner", mine)
    login(client, user)
    assert client.get(f"/manage/tenants/{mine.pk}/", secure=True).status_code == 200
    assert client.get(f"/manage/tenants/{other.pk}/", secure=True).status_code == 404
    page = client.get("/manage/tenants/", secure=True).content.decode()
    assert mine.name in page and other.name not in page
    add_totp  # noqa: B018 - imported helper kept for symmetry


def test_context_is_cleared_after_request(client, make_tenant) -> None:
    make_tenant()
    client.get("/health/live", secure=True)
    assert Tenant.objects.count() == 0
