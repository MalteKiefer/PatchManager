import pytest
from django.test import Client

from patchmanager.library.models import DeviceCategory
from patchmanager.locations.models import RoomType, RoomTypeNeed

pytestmark = pytest.mark.django_db


def test_builtin_entries_exist_and_translate() -> None:
    from django.utils import translation

    assert {"switch", "outlet", "other"} <= set(
        DeviceCategory.objects.values_list("key", flat=True)
    )
    office = RoomType.objects.get(key="office")
    assert office.needs.get(category_id="outlet").count == 4
    with translation.override("de"):
        assert office.label == "Büro"


def test_admins_shape_the_lists(admin_client: Client) -> None:
    assert admin_client.get("/manage/lists/", secure=True).status_code == 200
    created = admin_client.post(
        "/manage/lists/categories/new/",
        {"key": "sensor", "name": "Sensor", "glyph": "SE", "colour": "#0d9488", "order": 5},
        secure=True,
    )
    sensor = DeviceCategory.objects.get(key="sensor")
    assert created.url == f"/manage/lists/categories/{sensor.pk}/"
    duplicate = admin_client.post(
        "/manage/lists/categories/new/",
        {"key": "sensor", "name": "Again", "colour": "#000000", "order": 1},
        secure=True,
    )
    assert duplicate.status_code == 200 and DeviceCategory.objects.filter(key="sensor").count() == 1
    lab = admin_client.post(
        "/manage/lists/room-types/new/", {"key": "lab", "name": "Lab", "order": 1}, secure=True
    )
    room_type = RoomType.objects.get(key="lab")
    assert lab.url.endswith(f"/{room_type.pk}/")
    admin_client.post(
        f"/manage/lists/room-types/{room_type.pk}/needs/new/",
        {"category": "sensor", "count": 3},
        secure=True,
    )
    need = RoomTypeNeed.objects.get(room_type=room_type)
    assert need.count == 3
    page = admin_client.get(f"/manage/lists/room-types/{room_type.pk}/", secure=True).content
    assert b"Sensor" in page
    # in use (needed by a room type): the category stays
    admin_client.post(f"/manage/lists/categories/{sensor.pk}/", {"action": "delete"}, secure=True)
    assert not DeviceCategory.objects.filter(key="sensor").exists()  # needs cascade
    office = RoomType.objects.get(key="office")
    admin_client.post(f"/manage/lists/room-types/{office.pk}/", {"action": "delete"}, secure=True)
    other = RoomType.objects.get(key="other")
    response = admin_client.post(
        f"/manage/lists/room-types/{other.pk}/", {"action": "delete"}, secure=True, follow=True
    )
    assert b"cannot be deleted" in response.content


def test_categories_in_use_cannot_be_deleted(admin_client: Client, make_type) -> None:
    make_type("Thing", category="camera")
    camera = DeviceCategory.objects.get(key="camera")
    response = admin_client.post(
        f"/manage/lists/categories/{camera.pk}/", {"action": "delete"}, secure=True, follow=True
    )
    assert b"in use" in response.content and DeviceCategory.objects.filter(key="camera").exists()


def test_only_global_admins_change_lists(client: Client, make_user, grant, make_tenant) -> None:
    from tests.conftest import login

    user = make_user()
    grant(user, "editor", make_tenant())
    login(client, user)
    assert client.get("/manage/lists/categories/new/", secure=True).status_code == 403
