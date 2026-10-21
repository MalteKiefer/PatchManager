import pytest
from django.test import Client
from django.urls import reverse

from patchmanager.accounts.models import Assignment, Role
from patchmanager.accounts.services import PermissionDeniedError, ServiceError
from patchmanager.dcim.models import Rack
from patchmanager.history import services
from patchmanager.history.models import Change
from patchmanager.locations import services as tree
from patchmanager.locations.models import Room
from patchmanager.tenancy import rls
from tests.conftest import login

pytestmark = pytest.mark.django_db


def _changes(obj) -> list[Change]:
    with rls.system_context():
        return list(Change.objects.filter(object_id=obj.pk).order_by("occurred_at", "id"))


def test_triggers_record_insert_update_delete(make_tree, super_admin) -> None:
    t = make_tree()
    rack = t["rack"]
    with rls.system_context():
        rls.set_actor(super_admin.pk, "root@example.com", "req-12345678")
        tree.update(t["request"], super_admin, rack, name="A02", serial="SN1")
        Rack.objects.filter(pk=rack.pk).update(plan_x=10)  # bulk updates are recorded too
        Rack.objects.filter(pk=rack.pk).update(plan_x=10)  # no change, no row
        rls.set_actor(None)
    rows = _changes(rack)
    assert [c.action for c in rows] == ["insert", "update", "update"]
    assert rows[1].fields == ["name", "serial"]
    before, after = rows[1].before or {}, rows[1].after or {}
    assert (before["name"], after["name"]) == ("A01", "A02")
    assert rows[1].actor_label == "root@example.com" and rows[1].request_id == "req-12345678"
    assert str(rows[1].path) == str(rack.path) and rows[0].actor_id is None
    assert "path" not in after and "updated_at" not in after
    with rls.system_context():
        rack_pk, room_pk = rack.pk, t["room"].pk
        tree.delete(t["request"], super_admin, rack)
        tree.delete(t["request"], super_admin, t["room"])
        assert (
            Change.objects.filter(object_id=rack_pk).latest("occurred_at", "id").action == "delete"
        )
        assert (Change.objects.filter(object_id=room_pk).latest("occurred_at", "id").before or {})[
            "name"
        ] == "R1"


def test_restore_and_conflicts(make_tree, super_admin) -> None:
    t = make_tree()
    with rls.system_context():
        tree.update(t["request"], super_admin, t["rack"], name="A02", u_height=40)
        first = _changes(t["rack"])[-1]
        fields, skipped = services.restore(t["request"], super_admin, first)
        assert sorted(fields) == ["name", "u_height"] and skipped == []
        rack = Rack.objects.get(pk=t["rack"].pk)
        assert (rack.name, rack.u_height) == ("A01", 42)
        assert _changes(rack)[-1].fields == ["name", "u_height"]  # the restore is history too
        with pytest.raises(ServiceError, match="changed again"):
            services.restore(t["request"], super_admin, first)
        with pytest.raises(ServiceError, match="Only changes"):
            services.restore(t["request"], super_admin, _changes(rack)[0])
        moved = Change(
            object_type="dcim.rack", object_id=rack.pk, action="update", fields=["room_id"]
        )
        with pytest.raises(ServiceError, match="None of these"):
            services.restore(t["request"], super_admin, moved)


def test_visibility_follows_scope(make_tree, make_user, super_admin) -> None:
    t = make_tree()
    other = make_tree(name="Elsewhere")
    user = make_user()
    with rls.system_context():
        room2 = tree.create(t["request"], super_admin, Room, parent=t["floor"], name="Secret")
        Assignment.objects.create(
            role=Role.objects.get(key="editor"),
            user=user,
            tenant=t["tenant"],
            scope_path=t["room"].path,
        )
        seen = set(services.visible(user).values_list("object_id", flat=True))
        assert t["rack"].pk in seen and t["room"].pk in seen
        assert room2.pk not in seen and t["site"].pk not in seen and other["rack"].pk not in seen
        tree.update(t["request"], super_admin, room2, name="Secret 2")
        with pytest.raises(PermissionDeniedError):
            services.restore(t["request"], user, _changes(room2)[-1])
        with pytest.raises(ServiceError):
            services.for_object(user, "accounts.user", user.pk)


def test_conditional_grants_fail_closed(make_tree, make_user) -> None:
    t = make_tree()
    user = make_user()
    with rls.system_context():
        Assignment.objects.create(
            role=Role.objects.get(key="viewer"),
            user=user,
            tenant=t["tenant"],
            scope_path=t["tenant"].policy_path,
            conditions=[{"attr": "status", "op": "eq", "value": "active"}],
        )
        assert not services.visible(user).exists()


def test_views(make_tree, admin_client: Client, super_admin, make_user) -> None:
    t = make_tree()
    with rls.system_context():
        rls.set_actor(super_admin.pk, "root@example.com")
        tree.update(t["request"], super_admin, t["rack"], name="A02")
        rls.set_actor(None)
    change = _changes(t["rack"])[-1]
    page = admin_client.get(
        "/history/?type=dcim.rack&action=update&q=A0&since=2000-01-01", secure=True
    )
    assert page.status_code == 200 and "A02" in page.content.decode()
    assert (
        "A02"
        in admin_client.get(
            reverse("history:object", args=["dcim.rack", t["rack"].pk]), secure=True
        ).content.decode()
    )
    detail = admin_client.get(
        reverse("history:change", args=[change.pk]), secure=True
    ).content.decode()
    assert "Restore earlier values" in detail and "A01" in detail
    rack_page = admin_client.get(f"/racks/{t['rack'].pk}/", secure=True).content.decode()
    assert reverse("history:object", args=["dcim.rack", t["rack"].pk]) in rack_page
    response = admin_client.post(reverse("history:restore", args=[change.pk]), secure=True)
    assert response.status_code == 302
    with rls.system_context():
        assert Rack.objects.get(pk=t["rack"].pk).name == "A01"
        last = _changes(t["rack"])[-1]
    assert last.actor_label.startswith("root@example.com")  # set by the middleware
    again = admin_client.post(
        reverse("history:restore", args=[change.pk]), secure=True, follow=True
    )
    assert "changed again" in again.content.decode()
    assert (
        admin_client.get(
            reverse("history:object", args=["nope.x", t["rack"].pk]), secure=True
        ).status_code
        == 404
    )

    stranger = make_user()
    client = Client()  # admin_client is the default client
    login(client, stranger)
    with rls.system_context():
        assert not services.visible(stranger).exists()
    assert client.get(reverse("history:change", args=[change.pk]), secure=True).status_code == 404
    assert client.post(reverse("history:restore", args=[change.pk]), secure=True).status_code == 404


def test_tenant_delete_purges_history(make_tenant, super_admin) -> None:
    from django.test import RequestFactory
    from django.utils import timezone

    from patchmanager.locations.models import Site
    from patchmanager.tenancy import services as tenancy

    t = {"tenant": make_tenant(), "request": RequestFactory().post("/")}
    t["request"].user = super_admin
    with rls.system_context():
        site = tree.create(t["request"], super_admin, Site, parent=t["tenant"], name="Gone")
        assert _changes(site)
        tree.delete(t["request"], super_admin, site)
        t["tenant"].archived_at = timezone.now()
        t["tenant"].save()
        tenancy.delete_tenant(t["request"], super_admin, t["tenant"], confirm_slug=t["tenant"].slug)
        assert not Change.objects.filter(tenant_id=t["tenant"].pk).exists()


def test_restore_checks_resulting_state(make_tree, make_user, super_admin) -> None:
    t = make_tree()
    user = make_user()
    with rls.system_context():
        tree.update(t["request"], super_admin, t["rack"], status="planned")
        tree.update(t["request"], super_admin, t["rack"], status="active")
        change = _changes(t["rack"])[-1]  # planned -> active
        Assignment.objects.create(
            role=Role.objects.get(key="editor"),
            user=user,
            tenant=t["tenant"],
            scope_path=t["tenant"].policy_path,
            conditions=[{"attr": "status", "op": "eq", "value": "active"}],
        )
        with pytest.raises(PermissionDeniedError):
            services.restore(t["request"], user, change)  # would set status back to planned
        assert Rack.objects.get(pk=t["rack"].pk).status == "active"
