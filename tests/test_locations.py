import pytest
from django.db import connection, transaction
from django.db.utils import DatabaseError
from django.test import Client

from patchmanager.accounts import policy
from patchmanager.accounts.models import Assignment, Role
from patchmanager.accounts.services import PermissionDeniedError, ServiceError, rbac
from patchmanager.core.fields import path_label
from patchmanager.dcim.models import Rack
from patchmanager.locations import services
from patchmanager.locations.models import Building, Room, Site
from patchmanager.tenancy import rls
from tests.conftest import login

pytestmark = pytest.mark.django_db


def test_paths_and_tenant_inheritance(make_tree) -> None:
    t = make_tree()
    tenant, site, rack = t["tenant"], t["site"], t["rack"]
    assert site.path == f"{path_label(tenant.pk)}.{path_label(site.pk)}"
    assert str(rack.path).startswith(str(t["room"].path) + ".")
    assert rack.tenant_id == tenant.pk
    assert [str(a) for a in rack.ancestors()] == ["HQ", "B1", "F1", "R1"]


def test_trigger_rejects_foreign_parent(make_tree, system) -> None:
    a, b = make_tree(), make_tree(name="Other")
    with system(), pytest.raises(DatabaseError, match="tenant of its parent"), transaction.atomic():
        Building.objects.filter(pk=a["building"].pk).update(site=b["site"])


def test_trigger_rejects_forged_path(make_tree, system) -> None:
    t = make_tree()
    with (
        system(),
        pytest.raises(DatabaseError, match="path"),
        transaction.atomic(),
        connection.cursor() as c,
    ):
        c.execute("UPDATE locations_room SET path = 'aaaa.bbbb' WHERE id = %s", [t["room"].pk])


def test_rls_hides_other_tenants_nodes(make_tree) -> None:
    a, b = make_tree(), make_tree(name="Other")
    with rls.tenant_context([a["tenant"].pk]):
        assert list(Site.objects.all()) == [a["site"]]
        assert not Rack.objects.filter(pk=b["rack"].pk).exists()


def test_site_scope_inherits_down_not_sideways(make_tree, make_user, grant) -> None:
    t = make_tree()
    other = make_tree(t["tenant"], name="Branch")
    user = make_user()
    Assignment.objects.create(
        role=Role.objects.get(key="editor"),
        user=user,
        tenant=t["tenant"],
        scope_path=t["site"].path,
    )
    assert policy.has_perm(user, "dcim.rack.change", t["rack"])
    assert policy.has_perm(user, "locations.room.add", t["floor"])
    assert not policy.has_perm(user, "dcim.rack.change", other["rack"])
    with rls.system_context():
        visible = policy.scope_queryset(user, "dcim.rack.view", Rack.objects.all())
        assert list(visible) == [t["rack"]]


def test_unique_names_per_parent(make_tree, super_admin) -> None:
    t = make_tree()
    with rls.system_context(), pytest.raises(ServiceError, match="already exists"):
        services.create(t["request"], super_admin, Room, parent=t["floor"], name="r1")


def test_delete_requires_empty_node(make_tree, super_admin) -> None:
    t = make_tree()
    with rls.system_context():
        with pytest.raises(ServiceError, match="inside"):
            services.delete(t["request"], super_admin, t["room"])
        services.delete(t["request"], super_admin, t["rack"])
        services.delete(t["request"], super_admin, t["room"])
        assert not Room.objects.filter(pk=t["room"].pk).exists()


def test_permission_needed_to_create(make_tree, make_user, grant) -> None:
    t = make_tree()
    viewer = make_user()
    grant(viewer, "viewer", t["tenant"])
    with rls.system_context(), pytest.raises(PermissionDeniedError):
        services.create(t["request"], viewer, Building, parent=t["site"], name="B2")


def test_move_site_to_other_tenant(make_tree, make_tenant, make_user, super_admin, system) -> None:
    t = make_tree()
    target = make_tenant()
    with system():
        rbac.assign(
            t["request"],
            super_admin,
            role=Role.objects.get(key="editor"),
            user=make_user(),
            node=t["room"],
        )
    from patchmanager.dcim import services as dcim

    with system():
        dcim.reserve(
            t["request"], super_admin, t["rack"], start=1, size=2, face="both", label="Switch"
        )
        services.move_site(t["request"], super_admin, t["site"], target)
        rack = Rack.objects.get(pk=t["rack"].pk)
        assert rack.tenant_id == target.pk
        assert str(rack.path).startswith(path_label(target.pk))
        assert rack.spaces.get().tenant_id == target.pk
    moved = Assignment.objects.get(role__key="editor")
    assert moved.tenant_id == target.pk and str(moved.scope_path).startswith(path_label(target.pk))
    with rls.tenant_context([t["tenant"].pk]):
        assert not Site.objects.exists()
    with system(), pytest.raises(ServiceError):
        services.move_site(t["request"], super_admin, Site.objects.get(pk=t["site"].pk), target)


def test_assignment_scoped_to_room(make_tree, super_admin, make_user) -> None:
    t = make_tree()
    user = make_user()
    with rls.system_context():
        a = rbac.assign(
            t["request"],
            super_admin,
            role=Role.objects.get(key="viewer"),
            user=user,
            node=t["room"],
        )
    assert a.scope_label == f"{t['tenant']} / HQ / B1 / F1 / R1"
    assert policy.has_perm(user, "dcim.rack.view", t["rack"])
    assert not policy.has_perm(user, "locations.site.view", t["site"])
    assert policy.accessible_tenants(user) == (False, {t["tenant"].pk})


# --- Views ----------------------------------------------------------------


def test_index_map_and_create_site(admin_client: Client, make_tenant, system) -> None:
    tenant = make_tenant()
    admin_client.post("/tenants/switch/", {"tenant": tenant.pk}, secure=True)
    assert admin_client.get("/locations/", secure=True).status_code == 200
    response = admin_client.post(
        "/locations/sites/new/",
        {"name": "Campus", "latitude": "52.52", "longitude": "13.405"},
        secure=True,
    )
    assert response.status_code == 302
    page = admin_client.get("/locations/", secure=True).content.decode()
    assert "Campus" in page and '"lat": 52.52' in page
    bad = admin_client.post(
        "/locations/sites/new/", {"name": "Half", "latitude": "52"}, secure=True
    )
    assert bad.status_code == 200


def test_tree_navigation_and_crud(admin_client: Client, make_tree, system) -> None:
    t = make_tree()
    admin_client.post("/tenants/switch/", {"tenant": t["tenant"].pk}, secure=True)
    site_url = f"/locations/site/{t['site'].pk}/"
    page = admin_client.get(site_url, secure=True).content.decode()
    assert "R1" in page  # map -> site -> room in three clicks: room link on the site page
    admin_client.post(site_url, {"action": "add", "name": "B2"}, secure=True)
    with system():
        b2 = Building.objects.get(name="B2")
    admin_client.post(
        f"/locations/building/{b2.pk}/", {"action": "edit", "name": "B2 North"}, secure=True
    )
    admin_client.post(
        f"/locations/building/{b2.pk}/", {"action": "add", "name": "EG", "level": 0}, secure=True
    )
    room_url = f"/locations/room/{t['room'].pk}/"
    response = admin_client.get(room_url, secure=True)
    assert response.status_code == 200 and "A01" in response.content.decode()
    admin_client.post(
        room_url,
        {"action": "add", "name": "A02", "u_height": 24, "width": "19", "status": "active"},
        secure=True,
    )
    with system():
        assert Rack.objects.filter(name="A02", u_height=24).exists()
    response = admin_client.post(room_url, {"action": "delete"}, secure=True, follow=True)
    assert "Remove everything inside it first." in response.content.decode()
    assert (
        admin_client.get("/locations/nope/" + str(t["room"].pk) + "/", secure=True).status_code
        == 404
    )


def test_foreign_tenant_nodes_are_404(client: Client, make_tree, make_user, grant) -> None:
    mine, other = make_tree(), make_tree(name="Other")
    user = make_user()
    grant(user, "editor", mine["tenant"])
    login(client, user)
    assert client.get(f"/locations/site/{mine['site'].pk}/", secure=True).status_code == 200
    assert client.get(f"/locations/site/{other['site'].pk}/", secure=True).status_code == 404
    assert client.get(f"/racks/{other['rack'].pk}/", secure=True).status_code == 404


def test_viewer_cannot_change(client: Client, make_tree, make_user, grant, system) -> None:
    t = make_tree()
    user = make_user()
    grant(user, "viewer", t["tenant"])
    login(client, user)
    client.post(
        f"/locations/site/{t['site'].pk}/", {"action": "edit", "name": "Hacked"}, secure=True
    )
    with system():
        assert Site.objects.get(pk=t["site"].pk).name == "HQ"


def test_move_view(admin_client: Client, make_tree, make_tenant, system) -> None:
    t = make_tree()
    target = make_tenant()
    url = f"/locations/sites/{t['site'].pk}/move/"
    assert admin_client.get(url, secure=True).status_code == 200
    assert (
        admin_client.post(url, {"tenant": target.pk, "confirm": "wrong"}, secure=True).status_code
        == 200
    )
    assert (
        admin_client.post(url, {"tenant": target.pk, "confirm": "HQ"}, secure=True).status_code
        == 302
    )
    with system():
        assert Site.objects.get(pk=t["site"].pk).tenant_id == target.pk


def test_assignment_view_with_location(admin_client: Client, make_tree, make_user) -> None:
    t = make_tree()
    user = make_user()
    page = admin_client.get(f"/manage/users/{user.pk}/", secure=True).content.decode()
    assert f"rack:{t['rack'].pk}" in page
    admin_client.post(
        "/manage/assignments/",
        {
            "user": user.pk,
            "role": Role.objects.get(key="viewer").pk,
            "effect": "allow",
            "conditions": "[]",
            "location": f"rack:{t['rack'].pk}",
        },
        secure=True,
    )
    assert user.assignments.get().scope_path == t["rack"].path
    response = admin_client.post(
        "/manage/assignments/",
        {
            "user": user.pk,
            "role": Role.objects.get(key="viewer").pk,
            "effect": "allow",
            "conditions": "[]",
            "location": "rack:x",
        },
        secure=True,
    )
    assert response.status_code == 302  # not a valid choice: shown as form error
    assert user.assignments.count() == 1
