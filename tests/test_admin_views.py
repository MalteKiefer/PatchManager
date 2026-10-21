import pytest
from django.test import Client

from patchmanager.accounts.models import Group, Role, User
from patchmanager.tenancy.models import Tenant
from tests.conftest import login

pytestmark = pytest.mark.django_db

PAGES = [
    "/manage/",
    "/manage/users/",
    "/manage/users/invite/",
    "/manage/groups/",
    "/manage/roles/",
    "/manage/roles/new/",
    "/manage/permissions/",
    "/manage/permissions.json",
    "/manage/explain/",
    "/manage/tenants/",
    "/manage/tenants/new/",
    "/manage/audit/",
    "/manage/system/",
    "/account/",
    "/account/security/",
    "/manage/roles/export/?format=yaml",
]


@pytest.mark.parametrize("path", PAGES)
def test_pages_render_for_super_admin(admin_client: Client, path) -> None:
    assert admin_client.get(path, secure=True).status_code == 200


@pytest.mark.parametrize(
    "path", ["/manage/users/", "/manage/roles/", "/manage/system/", "/manage/audit/"]
)
def test_viewer_is_forbidden(client: Client, make_user, make_tenant, grant, path) -> None:
    user = make_user()
    grant(user, "viewer", make_tenant())
    login(client, user)
    assert client.get(path, secure=True).status_code == 403


def test_anonymous_redirected_to_login(client: Client, super_admin) -> None:
    assert client.get("/manage/", secure=True).url.startswith("/auth/login/")


def test_tenant_lifecycle(admin_client: Client, system) -> None:
    response = admin_client.post(
        "/manage/tenants/new/", {"slug": "acme", "name": "ACME", "save": "1"}, secure=True
    )
    assert response.status_code == 302
    with system():
        tenant = Tenant.objects.get(slug="acme")
    url = f"/manage/tenants/{tenant.pk}/"
    assert admin_client.get(url, secure=True).status_code == 200
    admin_client.post(url, {"save": "1", "slug": "acme", "name": "ACME Corp"}, secure=True)
    admin_client.post(url, {"delete": "1", "confirm": "acme"}, secure=True)  # not archived: refused
    admin_client.post(url, {"archive": "1"}, secure=True)
    admin_client.post(url, {"delete": "1", "confirm": "wrong"}, secure=True)
    with system():
        assert Tenant.objects.filter(pk=tenant.pk, name="ACME Corp").exists()
    admin_client.post(url, {"delete": "1", "confirm": "acme"}, secure=True)
    with system():
        assert not Tenant.objects.filter(pk=tenant.pk).exists()


def test_duplicate_tenant_slug(admin_client: Client, make_tenant) -> None:
    make_tenant(slug="dup")
    response = admin_client.post(
        "/manage/tenants/new/", {"slug": "dup", "name": "Dup", "save": "1"}, secure=True
    )
    assert response.status_code == 200


def test_switch_tenant(admin_client: Client, make_tenant) -> None:
    a, b = make_tenant(name="Aaa"), make_tenant(name="Bbb")
    admin_client.post(
        "/tenants/switch/", {"tenant": str(b.pk), "next": "https://evil.example/"}, secure=True
    )
    page = admin_client.get("/", secure=True).content.decode()
    assert "Current tenant: Bbb" in page
    a  # noqa: B018


def test_user_actions(admin_client: Client, make_user) -> None:
    target = make_user()
    base = f"/manage/users/{target.pk}/"
    assert admin_client.get(base, secure=True).status_code == 200
    for action in ("lock", "unlock", "deactivate", "activate", "reset-mfa", "revoke-sessions"):
        assert admin_client.post(base + action + "/", secure=True).status_code == 302
    assert admin_client.post(base + "bogus/", secure=True).status_code == 403
    target.refresh_from_db()
    assert target.is_active and not target.is_locked


def test_cannot_lock_self(admin_client: Client, super_admin) -> None:
    admin_client.post(f"/manage/users/{super_admin.pk}/lock/", secure=True)
    super_admin.refresh_from_db()
    assert not super_admin.is_locked


def test_impersonation(admin_client: Client, make_user, super_admin) -> None:
    target = make_user()
    response = admin_client.post(
        f"/manage/users/{target.pk}/impersonate/", {"reason": ""}, secure=True
    )
    assert response.url == f"/manage/users/{target.pk}/"
    admin_client.post(
        f"/manage/users/{target.pk}/impersonate/", {"reason": "Ticket 42"}, secure=True
    )
    page = admin_client.get("/", secure=True).content.decode()
    assert "are acting as" in page and target.email in page
    assert admin_client.post("/account/impersonation/stop/", secure=True).status_code == 302
    page = admin_client.get("/", secure=True).content.decode()
    assert "are acting as" not in page


def test_cannot_impersonate_super_admin(admin_client: Client, make_user, grant) -> None:
    other = make_user()
    grant(other, "super_admin")
    admin_client.post(f"/manage/users/{other.pk}/impersonate/", {"reason": "x"}, secure=True)
    assert "are acting as" not in admin_client.get("/", secure=True).content.decode()


def test_assignment_and_group_views(admin_client: Client, make_user, make_tenant) -> None:
    user = make_user()
    tenant = make_tenant()
    viewer = Role.objects.get(key="viewer")
    admin_client.post(
        "/manage/assignments/",
        {
            "user": user.pk,
            "role": viewer.pk,
            "tenant": tenant.pk,
            "effect": "allow",
            "conditions": "[]",
        },
        secure=True,
    )
    assignment = user.assignments.get()
    admin_client.post(f"/manage/assignments/{assignment.pk}/delete/", secure=True)
    assert not user.assignments.exists()
    admin_client.post("/manage/groups/new/", {"name": "NOC", "description": ""}, secure=True)
    group = Group.objects.get(name="NOC")
    other = Group.objects.create(name="All")
    url = f"/manage/groups/{group.pk}/"
    assert admin_client.get(url, secure=True).status_code == 200
    admin_client.post(url, {"action": "members", "members": [user.pk]}, secure=True)
    assert list(group.members.all()) == [user]
    admin_client.post(url, {"action": "members"}, secure=True)  # empty selection clears
    assert not group.members.exists()
    admin_client.post(url, {"action": "parent", "parent": other.pk}, secure=True)
    assert list(group.parents.all()) == [other]
    assert (
        admin_client.post(url, {"action": "remove_parent", "group": "x"}, secure=True).status_code
        == 404
    )
    admin_client.post(url, {"action": "remove_parent", "group": other.pk}, secure=True)
    bogus = {"user": "not-a-uuid", "role": "x"}
    assert admin_client.post("/manage/assignments/", bogus, secure=True).status_code == 302
    admin_client.post(url, {"action": "details", "name": "NOC2", "description": "x"}, secure=True)
    admin_client.post(url, {"action": "delete"}, secure=True)
    assert not Group.objects.filter(pk=group.pk).exists()


def test_role_views(admin_client: Client) -> None:
    admin_client.post(
        "/manage/roles/new/",
        {"name": "Patcher", "description": "", "permissions": ["tenancy.tenant.view"]},
        secure=True,
    )
    role = Role.objects.get(name="Patcher")
    admin_client.post(
        f"/manage/roles/{role.pk}/",
        {"name": "Patcher", "description": "x", "permissions": ["tenancy.tenant.change"]},
        secure=True,
    )
    role.refresh_from_db()
    assert role.permissions == ["tenancy.tenant.change"]
    admin_client.post(f"/manage/roles/{role.pk}/clone/", {"name": "Patcher 2"}, secure=True)
    assert Role.objects.filter(name="Patcher 2").exists()
    from django.core.files.uploadedfile import SimpleUploadedFile

    upload = SimpleUploadedFile(
        "r.yaml", b"version: 1\nroles:\n- name: Imported\n  permissions: []\n"
    )
    admin_client.post("/manage/roles/import/", {"file": upload}, secure=True)
    assert Role.objects.filter(name="Imported").exists()
    admin_client.post(f"/manage/roles/{role.pk}/delete/", secure=True)
    assert not Role.objects.filter(pk=role.pk).exists()


def test_explain_view(admin_client: Client, make_user, make_tenant) -> None:
    user = make_user()
    tenant = make_tenant()
    page = admin_client.get(
        f"/manage/explain/?user={user.pk}&permission=tenancy.tenant.view&tenant={tenant.pk}",
        secure=True,
    ).content.decode()
    assert "no matching assignment" in page


def test_user_list_hides_other_tenants_users(client: Client, make_user, make_tenant, grant) -> None:
    mine, other = make_tenant(), make_tenant()
    owner = make_user("owner@example.com")
    grant(owner, "tenant_owner", mine)
    colleague = make_user("colleague@example.com")
    grant(colleague, "viewer", mine)
    stranger = make_user("stranger@example.com")
    grant(stranger, "viewer", other)
    login(client, owner)
    page = client.get("/manage/users/", secure=True).content.decode()
    assert "colleague@example.com" in page and "stranger@example.com" not in page
    assert client.get(f"/manage/users/{stranger.pk}/", secure=True).status_code == 404
    assert User.objects.count() == 3
