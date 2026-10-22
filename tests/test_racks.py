import json

import pytest
from django.test import Client
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from patchmanager.accounts.services import ServiceError
from patchmanager.dcim import services
from patchmanager.dcim.elevation import UNIT, layout
from patchmanager.dcim.models import Rack, RackSpace
from patchmanager.tenancy import rls

pytestmark = pytest.mark.django_db


def test_faces_and_overlaps(make_tree, super_admin) -> None:
    t = make_tree()
    rack, req = t["rack"], t["request"]
    with rls.system_context():
        services.reserve(req, super_admin, rack, start=1, size=2, face="front", label="Patch panel")
        services.reserve(req, super_admin, rack, start=1, size=1, face="rear", label="PDU")
        with pytest.raises(ServiceError, match="taken"):
            services.reserve(req, super_admin, rack, start=2, size=1, face="both", label="Server")
        with pytest.raises(ServiceError, match="between 1 and 42"):
            services.reserve(
                req, super_admin, rack, start=42, size=2, face="front", label="Too high"
            )
        services.reserve(req, super_admin, rack, start=3, size=4, face="both", label="Server")
        stats = Rack.objects.get(pk=rack.pk).utilisation()
    assert stats == {"front": 6, "rear": 5, "used": 6, "free": 36, "percent": 14}


def test_move_and_release(make_tree, super_admin) -> None:
    t = make_tree()
    with rls.system_context():
        a = services.reserve(
            t["request"], super_admin, t["rack"], start=1, size=2, face="both", label="A"
        )
        services.reserve(
            t["request"], super_admin, t["rack"], start=10, size=1, face="both", label="B"
        )
        services.move(t["request"], super_admin, a, start=5)
        assert (a.start, a.end) == (5, 6)
        with pytest.raises(ServiceError):
            services.move(t["request"], super_admin, a, start=10)
        services.release(t["request"], super_admin, a)
        assert RackSpace.objects.count() == 1


def test_database_rejects_out_of_bounds_and_foreign_tenant(make_tree, system) -> None:
    from django.db import DatabaseError, transaction

    t, other = make_tree(), make_tree(name="Other")
    with system():
        space = RackSpace(rack=t["rack"], label="x")
        space.place(40, 5, "front")
        with pytest.raises(DatabaseError, match="outside the rack"), transaction.atomic():
            space.save()
        ok = RackSpace(rack=t["rack"], label="y")
        ok.place(1, 1, "front")
        ok.save()
        with pytest.raises(DatabaseError, match="tenant of its rack"), transaction.atomic():
            RackSpace.objects.filter(pk=ok.pk).update(tenant=other["tenant"])
        with pytest.raises(DatabaseError, match="cut off"), transaction.atomic():
            Rack.objects.filter(pk=t["rack"].pk).update(u_height=0)


@settings(
    max_examples=40, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(
    blocks=st.lists(
        st.tuples(
            st.integers(1, 12), st.integers(1, 4), st.sampled_from(["front", "rear", "both"])
        ),
        min_size=1,
        max_size=8,
    )
)
def test_exclusion_constraint_matches_python_model(make_tree, super_admin, blocks) -> None:
    """The database accepts a reservation exactly when it fits and overlaps nothing."""
    t = make_tree(u_height=12)
    taken: dict[str, set[int]] = {"front": set(), "rear": set()}
    with rls.system_context():
        for start, size, face in blocks:
            units = set(range(start, start + size))
            faces = ["front", "rear"] if face == "both" else [face]
            fits = start + size - 1 <= 12 and all(not (units & taken[f]) for f in faces)
            try:
                services.reserve(
                    t["request"],
                    super_admin,
                    t["rack"],
                    start=start,
                    size=size,
                    face=face,
                    label="b",
                )
                accepted = True
            except ServiceError:
                accepted = False
            assert accepted == fits
            if accepted:
                for f in faces:
                    taken[f] |= units
        RackSpace.objects.all().delete()


def test_elevation_coordinates(make_tree, super_admin) -> None:
    t = make_tree(u_height=10)
    with rls.system_context():
        services.reserve(
            t["request"], super_admin, t["rack"], start=1, size=2, face="front", label="Bottom"
        )
        rack = Rack.objects.get(pk=t["rack"].pk)
        front = layout(super_admin, rack, "front")
        assert front.bands[0].y == 8 * UNIT  # units 1-2 at the bottom of a 10 U rack
        assert layout(super_admin, rack, "rear").bands == []
        rack.descending_units = True
        assert layout(super_admin, rack, "front").bands[0].y == 0


def test_rack_page_and_json_move(admin_client: Client, make_tree, system) -> None:
    t = make_tree()
    url = f"/racks/{t['rack'].pk}/"
    assert admin_client.get(url + "spaces/new/", secure=True).status_code == 200
    admin_client.post(
        url + "spaces/new/",
        {"label": "Core switch", "start": 40, "size": 2, "face": "both"},
        secure=True,
    )
    page = admin_client.get(url + "?face=rear", secure=True).content.decode()
    assert "Core switch" in page and "rack-elevation" in page
    with system():
        space = RackSpace.objects.get()
    move_url = f"/racks/{t['rack'].pk}/spaces/{space.pk}/move/"
    ok = admin_client.post(
        move_url, json.dumps({"start": 20}), content_type="application/json", secure=True
    )
    assert ok.json() == {"start": 20, "end": 21, "face": "both"}
    assert (
        admin_client.post(
            move_url, json.dumps({"start": 42}), content_type="application/json", secure=True
        ).status_code
        == 409
    )
    assert (
        admin_client.post(
            move_url, "nope", content_type="application/json", secure=True
        ).status_code
        == 400
    )
    assert (
        admin_client.post(
            move_url,
            json.dumps({"start": 1, "face": "top"}),
            content_type="application/json",
            secure=True,
        ).status_code
        == 400
    )
    response = admin_client.post(
        url,
        {"action": "edit", "name": "A01", "u_height": 10, "width": "19", "status": "active"},
        secure=True,
        follow=True,
    )
    assert "conflicts with existing data" in response.content.decode()
    admin_client.post(f"/racks/{t['rack'].pk}/spaces/{space.pk}/delete/", secure=True)
    admin_client.post(url, {"action": "delete"}, secure=True)
    with system():
        assert not Rack.objects.filter(pk=t["rack"].pk).exists()


def test_viewer_cannot_move(client: Client, make_tree, make_user, grant, super_admin) -> None:
    from tests.conftest import login

    t = make_tree()
    with rls.system_context():
        space = services.reserve(
            t["request"], super_admin, t["rack"], start=1, size=1, face="front", label="x"
        )
    user = make_user()
    grant(user, "viewer", t["tenant"])
    login(client, user)
    page = client.get(f"/racks/{t['rack'].pk}/", secure=True).content.decode()
    assert "data-move-url" not in page
    response = client.post(
        f"/racks/{t['rack'].pk}/spaces/{space.pk}/move/",
        json.dumps({"start": 5}),
        content_type="application/json",
        secure=True,
    )
    assert response.status_code == 409


def test_conditions_judge_stored_state_not_submitted_values(make_tree, make_user) -> None:
    """A grant for planned racks must not cover an active rack submitted as planned."""
    from django.test import RequestFactory

    from patchmanager.accounts.models import Assignment, Role
    from patchmanager.accounts.services import PermissionDeniedError
    from patchmanager.locations import services as tree
    from patchmanager.locations.forms import RackForm

    t = make_tree()
    user = make_user()
    Assignment.objects.create(
        role=Role.objects.get(key="editor"),
        user=user,
        tenant=t["tenant"],
        scope_path=t["tenant"].policy_path,
        conditions=[{"attr": "status", "op": "eq", "value": "planned"}],
    )
    request = RequestFactory().post("/")
    request.user = user
    with rls.system_context():
        rack = Rack.objects.get(pk=t["rack"].pk)
        form = RackForm(
            {"name": "Renamed", "u_height": 42, "width": "19", "status": "planned"}, instance=rack
        )
        assert form.is_valid()  # mutates ``rack`` in memory
        with pytest.raises(PermissionDeniedError):
            tree.update(request, user, rack, **form.cleaned_data)
        assert Rack.objects.get(pk=rack.pk).name == "A01"
        Rack.objects.filter(pk=rack.pk).update(status="planned")
        with pytest.raises(PermissionDeniedError):  # resulting state leaves the grant
            tree.update(request, user, Rack.objects.get(pk=rack.pk), status="active")
        tree.update(request, user, Rack.objects.get(pk=rack.pk), name="Planned rack")


def test_rack_drawing_hides_devices_the_viewer_cannot_see(
    client: Client, super_admin, make_user, grant
) -> None:
    from patchmanager.accounts.models import Assignment, Role
    from patchmanager.dcim.management.commands.seed_scale import seed
    from patchmanager.dcim.models import Device
    from tests.conftest import login

    seed(super_admin, racks=1, slug="deny")
    with rls.system_context():
        switch = Device.objects.select_related("tenant").get(name="sw-0001")
        rack = Rack.objects.get(tenant__slug="deny")
    user = make_user()
    grant(user, "viewer", switch.tenant)
    Assignment.objects.create(
        role=Role.objects.get(key="viewer"),
        user=user,
        tenant=switch.tenant,
        scope_path=switch.path,
        effect="deny",
    )
    login(client, user)
    page = client.get(f"/racks/{rack.pk}/", secure=True).content.decode()
    assert "sw-0001" not in page and "not visible" in page
    assert "pp-0001" in page  # the panel is visible; its cords to the switch are not traced
    assert page.count('class="rack-port is-') == 24
