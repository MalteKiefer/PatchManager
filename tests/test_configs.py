import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from patchmanager.accounts.services import PermissionDeniedError, ServiceError
from patchmanager.audit.models import AuditEvent
from patchmanager.configs import services
from patchmanager.configs.models import DeviceConfig
from patchmanager.dcim import devices
from patchmanager.tenancy import rls

pytestmark = pytest.mark.django_db

V1 = "hostname sw1\nsnmp-server community s3cret RO\ninterface ge1\n description uplink\n"
V2 = "hostname sw1\nsnmp-server community s3cret RO\ninterface ge1\n description core uplink\n"


@pytest.fixture
def dev(make_tree, make_type):
    t = make_tree()
    with rls.system_context():
        device_type = make_type("Cfg switch")
        device = devices.create(
            t["request"], t["request"].user, device_type=device_type, room=t["room"], name="sw1"
        )
    return {**t, "device": device, "actor": t["request"].user}


def test_versions_are_deduplicated_encrypted_and_audited(dev) -> None:
    request, actor, device = dev["request"], dev["actor"], dev["device"]
    with rls.system_context():
        first, created = services.add(request, actor, device, V1, comment="initial")
        assert created and first.version == 1 and first.lines == 4
        same, created = services.add(request, actor, device, V1.replace("\n", "\r\n"))
        assert not created and same.pk == first.pk
        second, created = services.add(request, actor, device, V2)
        assert created and second.version == 2
        stored = DeviceConfig.objects.get(pk=first.pk).content
        assert "s3cret" not in stored  # encrypted at rest
        assert services.read(request, actor, first) == V1
        assert AuditEvent.objects.filter(action="configs.config.read").count() == 1
        lines, added, removed = services.diff(V1, V2)
        assert (added, removed) == (1, 1)
        assert [line.kind for line in lines if line.kind != "context"] == ["hunk", "remove", "add"]
        with pytest.raises(ServiceError):
            services.add(request, actor, device, "  \n")
        with pytest.raises(ServiceError):
            services.add(request, actor, device, "a\x00b")
        long = "\n".join(str(i) for i in range(services.MAX_DIFF_LINES + 1))
        with pytest.raises(ServiceError, match="too long to compare"):
            services.diff(long, long + "\nx")


def test_permissions(dev, make_user, grant) -> None:
    viewer = make_user()
    grant(viewer, "viewer", dev["tenant"])
    with rls.system_context():
        config, _new = services.add(dev["request"], dev["actor"], dev["device"], V1)
        with pytest.raises(PermissionDeniedError):
            services.add(dev["request"], viewer, dev["device"], V2)
        with pytest.raises(PermissionDeniedError):
            services.delete(dev["request"], viewer, config)
        assert services.read(dev["request"], viewer, config) == V1


def test_pages(admin_client, dev, system) -> None:
    device = dev["device"]
    base = f"/devices/{device.pk}/configs/"
    assert "No configuration stored yet." in admin_client.get(base, secure=True).content.decode()
    assert admin_client.get(base + "new/", secure=True).status_code == 200
    response = admin_client.post(base + "new/", {"content": V1, "comment": "first"}, secure=True)
    assert response.url == base + "1/"
    upload = SimpleUploadedFile("sw1.cfg", V2.encode())
    admin_client.post(base + "new/", {"file": upload, "comment": "second"}, secure=True)
    page = admin_client.get(base + "2/", secure=True).content.decode()
    assert "core uplink" in page and "Changes since v1" in page
    diff = admin_client.get(base + "diff/?a=1&b=2", secure=True).content.decode()
    assert "+1" in diff and "core uplink" in diff
    download = admin_client.get(base + "1/download/", secure=True)
    assert (
        download["Content-Disposition"].endswith('sw1-v1.txt"') and download.content == V1.encode()
    )
    assert "v2" in admin_client.get(f"/devices/{device.pk}/", secure=True).content.decode()
    admin_client.post(base + "1/delete/", secure=True)
    with system():
        assert list(DeviceConfig.objects.values_list("version", flat=True)) == [2]
    assert admin_client.get(base + "9/", secure=True).status_code == 404


def test_api(dev) -> None:
    from tests.test_api import _api, _get, _send, _token

    api = _api(_token(dev["actor"]))
    url = f"/api/v1/devices/{dev['device'].pk}/configs/"
    created = _send(api, "post", url, {"content": V1, "comment": "nightly"})
    assert created.status_code == 201 and created.json()["version"] == 1
    assert _send(api, "post", url, {"content": V1}).status_code == 200  # unchanged
    assert [c["version"] for c in _get(api, url).json()] == [1]
    one = _get(api, url + "1/").json()
    assert one["content"] == V1 and one["source"] == "api"
    assert _get(api, url + "7/").status_code == 404
    read_only = _api(_token(dev["actor"], ("read",)))
    assert _send(read_only, "post", url, {"content": V2}).status_code == 403


def test_configurations_follow_a_site_to_another_tenant(dev, make_tenant) -> None:
    from patchmanager.locations import services as tree

    target = make_tenant()
    with rls.system_context():
        config, _new = services.add(dev["request"], dev["actor"], dev["device"], V1)
        tree.move_site(dev["request"], dev["actor"], dev["site"], target)
        moved = DeviceConfig.objects.get(pk=config.pk)
        device = type(dev["device"]).objects.get(pk=dev["device"].pk)
        assert moved.tenant_id == target.pk and moved.path == device.path
