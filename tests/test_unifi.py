"""UniFi OS backups: reading without secrets, importing and updating, rights per part."""

import gzip
import io
import os
import struct
import tarfile
from typing import Any

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from patchmanager.accounts.models import Role
from patchmanager.accounts.roles import SYSTEM_ROLES
from patchmanager.cabling.models import Cable
from patchmanager.dcim.models import Device
from patchmanager.ipam.models import DHCPScope, IPAddress, Prefix
from patchmanager.library.models import Vendor
from patchmanager.networks import unifi, unifi_import
from patchmanager.networks.models import VLAN, WLAN, StaticRoute
from patchmanager.tenancy import rls
from tests.conftest import login

pytestmark = pytest.mark.django_db

SITE = "s1"
SECRET = "super-secret-passphrase"


def _bson(doc: dict[str, Any]) -> bytes:
    body = b""
    for key, value in doc.items():
        name = key.encode() + b"\0"
        if isinstance(value, bool):
            body += b"\x08" + name + (b"\x01" if value else b"\x00")
        elif isinstance(value, int):
            body += b"\x10" + name + struct.pack("<i", value)
        elif isinstance(value, str):
            raw = value.encode() + b"\0"
            body += b"\x02" + name + struct.pack("<i", len(raw)) + raw
        elif isinstance(value, dict):
            body += b"\x03" + name + _bson(value)
        elif isinstance(value, list):
            body += b"\x04" + name + _bson({str(i): v for i, v in enumerate(value)})
        elif value is None:
            body += b"\x0a" + name
    return struct.pack("<i", len(body) + 5) + body + b"\0"


def _db(collections: dict[str, list[dict[str, Any]]]) -> bytes:
    out = b""
    for name, rows in collections.items():
        out += _bson({"__cmd": "select", "collection": name})
        out += b"".join(_bson(r) for r in rows)
    return gzip.compress(out)


def _backup(collections: dict[str, list[dict[str, Any]]]) -> bytes:
    """A .unifi file as a UniFi OS console writes it."""
    tar_buffer = io.BytesIO()
    with tarfile.open(fileobj=tar_buffer, mode="w") as tar:
        db = _db(collections)
        info = tarfile.TarInfo("backup/network/db.gz")
        info.size = len(db)
        tar.addfile(info, io.BytesIO(db))
    plain = gzip.compress(tar_buffer.getvalue())
    plain += b"\0" * (-len(plain) % 16)  # zero padded, like the console does
    iv = os.urandom(16)
    encryptor = Cipher(algorithms.AES(unifi.KEY), modes.CBC(iv)).encryptor()
    return iv + encryptor.update(plain) + encryptor.finalize()


def _site(
    *, users_vlan: int = 10, security: str = "wpapsk", gateway_name: str = "GW"
) -> dict[str, Any]:
    lan: dict[str, Any] = {"_id": "n1", "site_id": SITE, "name": "Default", "purpose": "corporate"}
    users = {
        "_id": "n2",
        "site_id": SITE,
        "name": "Users",
        "purpose": "corporate",
        "vlan_enabled": True,
        "vlan": users_vlan,
        "ip_subnet": "10.10.0.1/24",
        "dhcpd_enabled": True,
        "dhcpd_start": "10.10.0.100",
        "dhcpd_stop": "10.10.0.200",
        "dhcpd_leasetime": 86400,
        "x_wan_password": SECRET,
    }
    lan |= {"ip_subnet": "192.168.1.1/24", "dhcpd_enabled": False}
    return {
        "site": [{"_id": SITE, "name": "default", "desc": "Office"}],
        "networkconf": [lan, users, {"_id": "w", "site_id": SITE, "purpose": "wan", "name": "WAN"}],
        "wlanconf": [
            {
                "site_id": SITE,
                "name": "Office",
                "security": security,
                "networkconf_id": "n2",
                "x_passphrase": SECRET,
            }
        ],
        "device": [
            {
                "site_id": SITE,
                "mac": "AA:00:00:00:00:01",
                "type": "udm",
                "model": "UDRULT",
                "name": gateway_name,
                "serial": "GW1",
                "ip": "192.168.1.1",
                "port_table": [{"port_idx": n} for n in range(1, 6)],
                "x_ssh_hostkey": SECRET,
            },
            {
                "site_id": SITE,
                "mac": "aa:00:00:00:00:02",
                "type": "usw",
                "model": "USL8LPB",
                "name": "SW",
                "serial": "SW1",
                "ip": "192.168.1.20",
                "port_table": [{"port_idx": n} for n in range(1, 9)],
                "port_overrides": [
                    {"port_idx": 2, "native_networkconf_id": "n2", "tagged_vlan_mgmt": "block_all"},
                    {"port_idx": 3, "name": "spare", "forward": "disabled"},
                ],
                "last_uplink": {
                    "uplink_mac": "aa:00:00:00:00:01",
                    "port_idx": 8,
                    "uplink_remote_port": 1,
                },
            },
        ],
        "user": [
            {
                "site_id": SITE,
                "mac": "bb:00:00:00:00:01",
                "use_fixedip": True,
                "fixed_ip": "10.10.0.50",
                "name": "printer",
            }
        ],
    }


def test_read_keeps_no_secrets_and_rejects_other_files() -> None:
    data = _backup(_site())
    backup = unifi.read(data)
    assert [d.name for d in backup.devices] == ["GW", "SW"]
    assert backup.devices[0].mac == "aa:00:00:00:00:01"
    assert backup.wlans[0].security == "wpa2-personal"
    assert [n.vid for n in backup.networks] == [1, 10, None]
    assert SECRET not in repr(backup)
    with pytest.raises(unifi.ReadError):
        unifi.read(os.urandom(4096))
    with pytest.raises(unifi.ReadError):
        unifi.read(data[:16] + data[16:-16][::-1] + data[-16:])


def test_import_creates_and_then_updates(make_tree, super_admin) -> None:
    tree = make_tree()
    request, tenant, room = tree["request"], tree["tenant"], tree["room"]
    backup = unifi.read(_backup(_site())).only(SITE)
    with rls.system_context():
        preview = unifi_import.run(request, super_admin, tenant, backup, room, commit=False)
        assert preview.changes and not Device.objects.filter(name="SW").exists()
        done = unifi_import.run(request, super_admin, tenant, backup, room, commit=True)
        assert "New cable GW Port 1 / SW Port 8" not in done.skipped
        switch = Device.objects.get(name="SW")
        assert switch.serial == "SW1"
        assert switch.ports.get(name="Port 1").mac_address == "aa:00:00:00:00:02"
        assert switch.device_type.model == "USL8LPB"
        users = VLAN.objects.get(tenant=tenant, vid=10)
        port2 = switch.ports.get(name="Port 2")
        assert (port2.untagged_vlan, port2.tagged.count()) == (users, 0)
        port1 = switch.ports.get(name="Port 1")
        assert port1.untagged_vlan.vid == 1 and [t.vlan.vid for t in port1.tagged.all()] == [10]
        assert not switch.ports.get(name="Port 3").enabled
        assert switch.ports.get(name="Port 3").label == "spare"
        assert IPAddress.objects.get(host="192.168.1.20").port.device == switch
        prefix = Prefix.objects.get(tenant=tenant, prefix="10.10.0.0/24")
        assert (str(prefix.gateway).split("/")[0], prefix.vlan) == ("10.10.0.1", users)
        scope = DHCPScope.objects.get(tenant=tenant, name="Users")
        assert scope.server.name == "GW"
        assert [str(r.address).split("/")[0] for r in scope.reservations.all()] == ["10.10.0.50"]
        assert WLAN.objects.get(tenant=tenant, ssid="Office").vlan == users
        assert Cable.objects.filter(tenant=tenant).count() == 1

        StaticRoute.objects.create(  # documented by hand: the backup knows no routes
            tenant=tenant,
            device=Device.objects.get(name="GW"),
            prefix="0.0.0.0/0",
            next_hop="192.0.2.1",
        )
        again = unifi.read(_backup(_site(security="open", gateway_name="GW-2"))).only(SITE)
        changed = unifi_import.run(request, super_admin, tenant, again, room, commit=True)
        assert "Device GW-2 changed" in changed.changes
        assert WLAN.objects.get(tenant=tenant, ssid="Office").security == "open"
        assert Device.objects.filter(tenant=tenant).count() == 2  # matched, not duplicated
        assert Cable.objects.filter(tenant=tenant).count() == 1
        assert not unifi_import.run(request, super_admin, tenant, again, room, commit=True).changes
        assert StaticRoute.objects.filter(device__name="GW-2").count() == 1


def test_parts_need_their_import_right(make_tree, make_user, grant) -> None:
    tree = make_tree()
    Role.objects.create(
        key="no-wlan-import",
        name="No WLAN import",
        permissions=sorted(
            p for p in SYSTEM_ROLES["editor"].permissions() if p != "networks.wlan.import"
        ),
    )
    user = make_user()
    grant(user, "no-wlan-import", tree["tenant"])
    backup = unifi.read(_backup(_site())).only(SITE)
    with rls.system_context():
        preview = unifi_import.run(
            tree["request"], user, tree["tenant"], backup, tree["room"], commit=False
        )
        assert any("Ubiquiti is not in the device library" in s for s in preview.skipped)
        Vendor.objects.create(name="Ubiquiti")  # shared: added by an administrator
        result = unifi_import.run(
            tree["request"], user, tree["tenant"], backup, tree["room"], commit=True
        )
        assert "You may not import WLANs." in result.skipped
        assert not WLAN.objects.filter(tenant=tree["tenant"]).exists()
        assert Device.objects.filter(tenant=tree["tenant"], name="SW").exists()


def test_page_previews_then_applies(client: Client, make_tree, make_user, grant) -> None:
    tree = make_tree()
    user = make_user()
    grant(user, "editor", tree["tenant"])
    login(client, user)
    client.post("/tenants/switch/", {"tenant": tree["tenant"].pk}, secure=True)
    with rls.system_context():
        Vendor.objects.create(name="Ubiquiti")
    url = "/networks/unifi/import/"
    assert client.get(url, secure=True).status_code == 200
    upload = SimpleUploadedFile("backup.unifi", _backup(_site()))
    page = client.post(
        url, {"file": upload, "room": tree["room"].pk, "action": "preview"}, secure=True
    )
    assert "New device SW" in page.content.decode()
    with rls.system_context():
        assert not Device.objects.filter(name="SW").exists()
    done = client.post(url, {"room": tree["room"].pk, "action": "apply"}, secure=True)
    assert done.status_code == 302
    with rls.system_context():
        assert Device.objects.filter(tenant=tree["tenant"], name="SW").exists()
    bad = SimpleUploadedFile("backup.unifi", os.urandom(512))
    page = client.post(
        url, {"file": bad, "room": tree["room"].pk, "action": "preview"}, secure=True
    )
    assert "This is not a UniFi OS backup" in page.content.decode()


def test_page_needs_an_import_right(client: Client, make_tree, make_user, grant) -> None:
    tree = make_tree()
    user = make_user()
    grant(user, "viewer", tree["tenant"])
    login(client, user)
    client.post("/tenants/switch/", {"tenant": tree["tenant"].pk}, secure=True)
    assert client.get("/networks/unifi/import/", secure=True).status_code == 403
