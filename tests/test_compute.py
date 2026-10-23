import socket
from typing import Any

import pytest
from django.test import Client

from patchmanager.accounts.services import ServiceError
from patchmanager.compute import connections, inventory
from patchmanager.compute.models import ClusterHost, Connection, VirtualMachine
from patchmanager.core.fields import path_label
from patchmanager.dcim.models import Device

pytestmark = pytest.mark.django_db

TOKEN = "monitor@pve!patchmanager=12345678-1234-1234-1234-123456789abc"


class FakePVE:
    """The answers of a small Proxmox VE cluster; ``nodes`` says where each guest runs."""

    def __init__(self) -> None:
        self.nodes = {"100": "pve1", "101": "pve2", "200": "pve1"}
        self.extra: list[dict[str, Any]] = []

    def answer(self, path: str) -> Any:
        if path == "/api2/json/cluster/status":
            return {
                "data": [
                    {"type": "cluster", "name": "LAB"},
                    {"type": "node", "name": "pve1"},
                    {"type": "node", "name": "pve2"},
                ]
            }
        if path == "/api2/json/cluster/resources":
            guests = [
                {
                    "type": "qemu",
                    "vmid": 100,
                    "name": "web",
                    "node": self.nodes["100"],
                    "status": "running",
                    "maxcpu": 2,
                    "maxmem": 4 * 1024**3,
                    "maxdisk": 32 * 1024**3,
                },
                {
                    "type": "qemu",
                    "vmid": 101,
                    "name": "db",
                    "node": self.nodes["101"],
                    "status": "stopped",
                    "maxcpu": 4,
                    "maxmem": 8 * 1024**3,
                },
                {
                    "type": "lxc",
                    "vmid": 200,
                    "name": "dns",
                    "node": self.nodes["200"],
                    "status": "running",
                },
                *self.extra,
            ]
            return {
                "data": [
                    *guests,
                    {
                        "type": "storage",
                        "storage": "local-lvm",
                        "node": "pve1",
                        "maxdisk": 500 * 1024**3,
                        "disk": 100 * 1024**3,
                        "plugintype": "lvmthin",
                    },
                ]
            }
        if path.endswith("/qemu/100/config"):
            return {
                "data": {
                    "ostype": "l26",
                    "net0": "virtio=AA:BB:CC:00:00:01,bridge=vmbr0,tag=10",
                    "scsi0": "local-lvm:vm-100-disk-0,size=32G",
                }
            }
        if path.endswith("/lxc/200/config"):
            return {
                "data": {
                    "ostype": "debian",
                    "net0": "name=eth0,bridge=vmbr0,hwaddr=AA:BB:CC:00:00:02,ip=10.0.0.53/24",
                }
            }
        return {"data": {}}


@pytest.fixture
def pve(monkeypatch):
    fake = FakePVE()
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda host, port, **kw: [(2, 1, 6, "", ("10.0.0.10", port))]
    )
    monkeypatch.setattr(inventory.Client, "get", lambda self, path, headers=None: fake.answer(path))
    return fake


def test_proxmox_inventory(pve) -> None:
    found = inventory.proxmox("https://pve1.lab:8006", TOKEN)
    assert found.cluster == "LAB" and found.hosts == ["pve1", "pve2"]
    web = next(g for g in found.guests if g.external_id == "100")
    assert (web.memory_mb, web.disk_gb, web.operating_system) == (4096, 32, "l26")
    assert web.nics[0] == {
        "name": "net0",
        "mac": "aa:bb:cc:00:00:01",
        "bridge": "vmbr0",
        "vlan": "10",
        "addresses": None,  # VMs do not carry their address in the configuration
    }
    assert web.disks == [("scsi0", 32, "local-lvm")]
    dns = next(g for g in found.guests if g.external_id == "200")
    assert dns.kind == "container" and dns.nics[0]["addresses"] == "10.0.0.53/24"
    assert found.storage[0].size_gb == 500


def test_addresses_and_tokens_are_checked(monkeypatch) -> None:
    with pytest.raises(ServiceError):
        inventory.proxmox("http://pve.lab:8006", TOKEN)  # https only
    with pytest.raises(ServiceError):
        inventory.proxmox("https://pve.lab", "not a token")
    for address in ("127.0.0.1", "169.254.169.254", "0.0.0.0", "::1"):  # noqa: S104
        monkeypatch.setattr(
            socket, "getaddrinfo", lambda h, p, a=address, **kw: [(2, 1, 6, "", (a, p))]
        )
        with pytest.raises(ServiceError, match="cannot be used"):
            inventory.Client("https://pve.lab:8006")
    monkeypatch.setattr(socket, "getaddrinfo", lambda h, p, **kw: [(2, 1, 6, "", ("10.0.0.10", p))])
    with pytest.raises(ServiceError, match="fingerprint"):
        inventory.Client("https://pve.lab:8006", "abc")
    inventory.Client("https://pve.lab:8006", "ab" * 32)  # a valid pin


def test_connect_choose_and_follow_migrations(
    admin_client: Client, make_tree, make_type, pve, system
) -> None:
    tree = make_tree()
    admin_client.post("/tenants/switch/", {"tenant": tree["tenant"].pk}, secure=True)
    with system():
        server_type = make_type("PowerEdge R650", u_height=1)
        from patchmanager.dcim import devices

        devices.create(
            tree["request"],
            tree["request"].user,
            device_type=server_type,
            room=tree["room"],
            name="pve1",
        )
    created = admin_client.post(
        "/compute/connections/new/",
        {"name": "Lab", "url": "https://pve1.lab:8006", "fingerprint": "", "token": TOKEN},
        secure=True,
    )
    with system():
        connection = Connection.objects.get()
        assert TOKEN not in connection.secret  # stored encrypted
    assert created.url == f"/compute/connections/{connection.pk}/select/"
    page = admin_client.get(created.url, secure=True).content.decode()
    assert "pve2 (new server)" in page and "pve1 (documented)" in page and "100 · web" in page
    admin_client.post(
        created.url,
        {
            "nodes": ["pve1", "pve2"],
            "guests": ["100", "101"],
            "storage": ["local-lvm"],
            "add_new_guests": "on",
            "node_room": tree["room"].pk,
            "node_type": server_type.pk,
            "interval": "6",
        },
        secure=True,
    )
    with system():
        assert Device.objects.filter(name="pve2").exists()  # a node became a server
        assert ClusterHost.objects.filter(cluster__name="LAB").count() == 2
        web = VirtualMachine.objects.get(external_id="100")
        assert web.host.name == "pve1" and web.interfaces.get().vlan is None
        assert not VirtualMachine.objects.filter(external_id="200").exists()  # left out
        assert web.disks.get().storage.name == "local-lvm"
    pve.nodes["100"] = "pve2"  # live migration
    pve.extra = [{"type": "qemu", "vmid": 102, "name": "new", "node": "pve1", "status": "running"}]
    admin_client.post(f"/compute/connections/{connection.pk}/", {"action": "sync"}, secure=True)
    with system():
        assert VirtualMachine.objects.filter(external_id="100").count() == 1
        assert VirtualMachine.objects.get(external_id="100").host.name == "pve2"
        assert VirtualMachine.objects.filter(external_id="102").exists()  # new machine taken over
        assert not VirtualMachine.objects.filter(external_id="200").exists()  # still left out
        connection.refresh_from_db()
        assert "102" in connection.guests and connection.last_error == ""
    detail = admin_client.get(
        f"/compute/connections/{connection.pk}/", secure=True
    ).content.decode()
    assert "LAB" in detail and "updated machines" in detail


def test_scheduled_update(make_tree, super_admin, pve, system) -> None:
    from django.utils import timezone

    from patchmanager.core import crypto

    tree = make_tree()
    with system():
        connection = Connection.objects.create(
            tenant=tree["tenant"],
            created_by=super_admin,
            name="Lab",
            url="https://pve1.lab:8006",
            secret=crypto.encrypt(TOKEN),
            nodes=[],
            guests=["100"],
            storage=[],
            seen=["100", "101", "200"],
            add_new_guests=False,
            interval=1,
        )
        Connection.objects.create(
            tenant=tree["tenant"],
            created_by=super_admin,
            name="Fresh",
            url="https://pve1.lab:8006",
            secret=crypto.encrypt(TOKEN),
            interval=1,
            last_synced_at=timezone.now(),
        )
    assert connections.sync_due() == 1  # the fresh one is not due
    with system():
        assert list(VirtualMachine.objects.values_list("external_id", flat=True)) == ["100"]
        connection.refresh_from_db()
        assert connection.last_synced_at is not None


def test_vmware_and_synology(
    admin_client: Client, make_tree, make_type, monkeypatch, system
) -> None:
    from patchmanager.compute.models import StorageVolume
    from patchmanager.dcim import devices

    monkeypatch.setattr(socket, "getaddrinfo", lambda h, p, **kw: [(2, 1, 6, "", ("10.0.0.20", p))])
    calls: list[tuple[str, str]] = []

    def request(self, method, path, *, headers=None, body=None):
        calls.append((method, path))
        if path == "/api/session":
            return (201, "session-1") if method == "POST" else (204, None)
        if path.startswith("/webapi/entry.cgi") and method == "POST":
            return 200, {"success": True, "data": {"sid": "s1"}}
        return 200, None

    answers = {
        "/api/vcenter/cluster": [{"name": "VC-CL"}],
        "/api/vcenter/host": [{"host": "host-1", "name": "esx1"}],
        "/api/vcenter/vm?hosts=host-1": [
            {
                "vm": "vm-7",
                "name": "app",
                "power_state": "POWERED_ON",
                "cpu_count": 2,
                "memory_size_MiB": 2048,
            }
        ],
        "/api/vcenter/vm/vm-7": {
            "guest_OS": "UBUNTU_64",
            "nics": {
                "4000": {
                    "label": "Network adapter 1",
                    "mac_address": "00:50:56:00:00:01",
                    "backing": {"network_name": "VM Network"},
                }
            },
            "disks": {"2000": {"label": "Hard disk 1", "capacity": 40 * 1024**3}},
        },
        "/api/vcenter/datastore": [
            {"name": "ds1", "type": "VMFS", "capacity": 1024**4, "free_space": 512 * 1024**3}
        ],
    }

    def get(self, path, headers=None):
        if path.startswith("/webapi/entry.cgi"):
            if "SYNO.Storage.CGI.Storage" in path:
                return {
                    "success": True,
                    "data": {
                        "volumes": [
                            {
                                "vol_path": "/volume1",
                                "size": {"total": str(4 * 1024**4), "used": str(1024**4)},
                            }
                        ],
                        "storagePools": [],
                    },
                }
            return {
                "success": True,
                "data": {"shares": [{"name": "backup", "vol_path": "/volume1"}]},
            }
        return answers[path]

    monkeypatch.setattr(inventory.Client, "request", request)
    monkeypatch.setattr(inventory.Client, "get", get)
    tree = make_tree()
    admin_client.post("/tenants/switch/", {"tenant": tree["tenant"].pk}, secure=True)
    page = admin_client.post(
        "/compute/import/",
        {"platform": "vmware", "url": "https://vc.lab", "username": "ro", "password": "pw"},
        secure=True,
    )
    assert page.status_code == 302
    with system():
        app = VirtualMachine.objects.get(external_id="vm-7")
        assert (app.cluster.name, app.memory_mb, app.disk_gb) == ("VC-CL", 2048, 40)
        assert app.interfaces.get().bridge == "VM Network"
        assert StorageVolume.objects.get(name="ds1").used_gb == 512
        nas = devices.create(
            tree["request"],
            tree["request"].user,
            device_type=make_type("DS920+"),
            room=tree["room"],
            rack=tree["rack"],
            position=5,
            name="NAS-1",
        )
    assert ("DELETE", "/api/session") in calls  # signed out again
    # A second cluster keeps the documented name, so a re-import finds the same machines.
    answers["/api/vcenter/cluster"] = [{"name": "VC-CL"}, {"name": "VC-B"}]
    admin_client.post(
        "/compute/import/",
        {"platform": "vmware", "url": "https://vc.lab", "username": "ro", "password": "pw"},
        secure=True,
    )
    with system():
        assert VirtualMachine.objects.get(external_id="vm-7").cluster.name == "VC-CL"
    admin_client.post(
        "/compute/import/",
        {
            "platform": "synology",
            "url": "https://nas.lab:5001",
            "username": "ro",
            "password": "pw",
            "nas": nas.pk,
        },
        secure=True,
    )
    with system():
        volume = StorageVolume.objects.get(name="/volume1")
        assert (volume.device_id, volume.size_gb, volume.used_gb) == (nas.pk, 4096, 1024)
        assert StorageVolume.objects.get(name="backup").kind == "smb"
    device_page = admin_client.get(f"/devices/{nas.pk}/", secure=True).content.decode()
    assert "/volume1" in device_page
    missing = admin_client.post(
        "/compute/import/", {"platform": "proxmox", "url": "https://p.lab"}, secure=True
    )
    assert "Enter the API token" in missing.content.decode()


def test_compute_pages(admin_client: Client, make_tree, system) -> None:
    tree = make_tree()
    admin_client.post("/tenants/switch/", {"tenant": tree["tenant"].pk}, secure=True)
    _ok = admin_client.post("/items/clusters/new/", {"name": "C1", "kind": "kvm"}, secure=True)
    with system():
        from patchmanager.compute.models import Cluster

        cluster = Cluster.objects.get()
    admin_client.post(
        "/items/vms/new/",
        {"name": "vm1", "kind": "vm", "status": "running", "cluster": cluster.pk},
        secure=True,
    )
    with system():
        vm = VirtualMachine.objects.get()
    admin_client.post(
        "/items/vm-interfaces/new/", {"parent": vm.pk, "name": "eth0", "enabled": "on"}, secure=True
    )
    admin_client.post(
        "/items/vm-disks/new/", {"parent": vm.pk, "name": "sda", "size_gb": 20}, secure=True
    )
    page = admin_client.get(f"/items/vms/{vm.pk}/", secure=True).content.decode()
    assert "eth0" in page and "sda" in page
    for url in (
        "/items/vms/",
        "/items/clusters/",
        "/items/storage/",
        "/compute/connections/",
        "/compute/connections/new/",
        "/compute/import/",
    ):
        assert admin_client.get(url, secure=True).status_code == 200, url


def test_one_broken_connection_does_not_stop_the_others(
    make_tree, super_admin, pve, system, monkeypatch
) -> None:
    from patchmanager.compute import tasks
    from patchmanager.core import crypto

    tree = make_tree()
    with system():
        broken = Connection.objects.create(
            tenant=tree["tenant"],
            created_by=super_admin,
            name="Broken",
            url="https://pve1.lab:8006",
            secret="not-encrypted",
            interval=1,
        )
        good = Connection.objects.create(
            tenant=tree["tenant"],
            created_by=super_admin,
            name="Good",
            url="https://pve1.lab:8006",
            secret=crypto.encrypt(TOKEN),
            interval=1,
            guests=["100"],
            seen=["100", "101", "200"],
            add_new_guests=False,
        )
    started = []
    monkeypatch.setattr(tasks.sync_connection, "delay", lambda pk: started.append(pk))
    assert tasks.sync_connections() == 2 and len(started) == 2
    assert connections.due() == []  # claimed: a second run starts nothing
    assert connections.sync_one(broken.pk) is False  # an unreadable token is recorded
    assert connections.sync_one(good.pk) is True
    with system():
        broken.refresh_from_db()
        assert broken.last_error
        assert VirtualMachine.objects.filter(external_id="100").exists()


QNAP_USAGE = b"""<?xml version="1.0" encoding="UTF-8"?>
<QDocRoot>
  <volumeList><volume><volumeValue>1</volumeValue><volumeLabel>DataVol1</volumeLabel></volume></volumeList>
  <volumeUseList><volumeUse>
    <volumeValue>1</volumeValue>
    <total_size>4398046511104</total_size><free_size>3298534883328</free_size>
    <folder_element><sharename>Public</sharename><used_size>10737418240</used_size></folder_element>
    <folder_element><sharename>backup</sharename><used_size>0</used_size></folder_element>
  </volumeUse></volumeUseList>
</QDocRoot>"""


def test_qnap(admin_client: Client, make_tree, make_type, monkeypatch, system) -> None:
    from patchmanager.compute.models import StorageVolume
    from patchmanager.dcim import devices

    monkeypatch.setattr(socket, "getaddrinfo", lambda h, p, **kw: [(2, 1, 6, "", ("10.0.0.30", p))])
    calls: list[tuple[str, str, str | None]] = []
    passed = {"value": "1"}

    def fetch(self, method, path, *, headers=None, body=None):
        calls.append((method, path, body))
        if path == "/cgi-bin/authLogin.cgi":
            return 200, (
                f"<QDocRoot><authPassed><![CDATA[{passed['value']}]]></authPassed>"
                "<authSid><![CDATA[sid-9]]></authSid></QDocRoot>"
            ).encode()
        if path.startswith("/cgi-bin/management/chartReq.cgi"):
            return 200, QNAP_USAGE
        return 200, b"<QDocRoot/>"

    monkeypatch.setattr(inventory.Client, "fetch", fetch)
    tree = make_tree()
    admin_client.post("/tenants/switch/", {"tenant": tree["tenant"].pk}, secure=True)
    with system():
        nas = devices.create(
            tree["request"],
            tree["request"].user,
            device_type=make_type("TS-873A"),
            room=tree["room"],
            rack=tree["rack"],
            position=7,
            name="NAS-Q",
        )
    form = {
        "platform": "qnap",
        "url": "https://qnap.lab",
        "username": "ro",
        "password": "pw!",
        "nas": nas.pk,
    }
    assert admin_client.post("/compute/import/", form, secure=True).status_code == 302
    with system():
        volume = StorageVolume.objects.get(name="DataVol1")
        assert (volume.device_id, volume.size_gb, volume.used_gb) == (nas.pk, 4096, 1024)
        share = StorageVolume.objects.get(name="Public")
        assert (share.kind, share.used_gb, share.target) == ("smb", 10, "DataVol1")
    login = calls[0]
    assert login[0] == "POST" and "pwd=cHch" in (login[2] or "")  # base64, in the body
    assert all("cHch" not in path for _, path, _ in calls)
    assert calls[-1][1] == "/cgi-bin/authLogout.cgi?sid=sid-9"

    passed["value"] = "0"
    refused = admin_client.post("/compute/import/", form, secure=True).content.decode()
    assert "two step sign in" in refused


def test_vm_addresses_are_linked_in_ipam(
    admin_client: Client, make_tree, super_admin, system
) -> None:
    from patchmanager.compute import importing
    from patchmanager.compute.models import VMInterface
    from patchmanager.ipam.models import IPAddress

    tree = make_tree()
    tenant, request = tree["tenant"], tree["request"]
    with system():
        free = IPAddress.objects.create(
            tenant=tenant, address="10.0.0.6/24", path=path_label(tenant.pk)
        )
    nic = {"name": "net0", "mac": "bc:24:11:00:00:01", "bridge": "vmbr0", "vlan": ""}

    def platform(addresses: str) -> inventory.Inventory:
        guest = inventory.Guest("100", "web", nics=[{**nic, "addresses": addresses}])
        return inventory.Inventory("proxmox", cluster="PVE", guests=[guest])

    with system():
        first = importing.save(
            request, super_admin, tenant, platform("10.0.0.5/24, 10.0.0.6/24, fe80::1/64")
        )
        card = VMInterface.objects.get(vm__external_id="100")
        linked = sorted(a.host for a in IPAddress.objects.filter(vm_interface=card))
        assert linked == ["10.0.0.5", "10.0.0.6"]  # created and taken over; link local left out
    assert first.created == 1

    page = admin_client.get(f"/ipam/addresses/{free.pk}/", secure=True).content.decode()
    assert "web" in page and "net0" in page
    card_page = admin_client.get(f"/items/vm-interfaces/{card.pk}/", secure=True).content.decode()
    assert "10.0.0.5/24" in card_page and "10.0.0.6/24" in card_page

    with system():
        importing.save(request, super_admin, tenant, platform("10.0.0.5/24"))
        assert VMInterface.objects.get(vm__external_id="100").pk == card.pk  # updated in place
        free.refresh_from_db()
        assert free.vm_interface_id is None  # no longer reported: unlinked, record kept
    both = inventory.Inventory(
        "proxmox",
        cluster="PVE",
        guests=[
            inventory.Guest("100", "web", nics=[{**nic, "addresses": "10.0.0.5/24"}]),
            inventory.Guest("101", "db", nics=[{**nic, "addresses": "10.0.0.5/24"}]),
        ],
    )
    with system():
        clash = importing.save(request, super_admin, tenant, both)
    assert "10.0.0.5 of db is already used elsewhere and was not linked." in clash.notes


def test_connections_keep_clusters_and_manual_cards(
    admin_client: Client, make_tree, super_admin, make_user, pve, system
) -> None:
    from patchmanager.compute.models import VMInterface
    from patchmanager.core import crypto

    tree = make_tree()
    admin_client.post("/tenants/switch/", {"tenant": tree["tenant"].pk}, secure=True)
    fields = {
        "tenant": tree["tenant"],
        "url": "https://pve1.lab:8006",
        "secret": crypto.encrypt(TOKEN),
        "guests": ["100"],
        "seen": ["100", "101", "200"],
    }
    with system():
        first = Connection.objects.create(created_by=super_admin, name="A", **fields)
        second = Connection.objects.create(created_by=make_user(), name="B", **fields)
        connections.sync(first, super_admin)
        card = VMInterface.objects.get(vm__external_id="100")
        card.addresses = "10.5.0.5/24"  # documented by hand; Proxmox VMs report none
        card.save()
        connections.sync(first, super_admin)
        assert VMInterface.objects.get(pk=card.pk).addresses == "10.5.0.5/24"
        with pytest.raises(ServiceError, match="already updated by the connection A"):
            connections.sync(second, super_admin)
    admin_client.post(
        f"/compute/connections/{second.pk}/select/",
        {"guests": ["100"], "interval": "0"},
        secure=True,
    )
    with system():
        second.refresh_from_db()
        assert second.created_by == super_admin  # scheduled runs act as the last editor


def test_proxmox_reports_removed_tags_and_container_addresses(
    make_tree, super_admin, pve, system
) -> None:
    from patchmanager.compute.models import VMInterface
    from patchmanager.core import crypto
    from patchmanager.ipam.models import IPAddress
    from patchmanager.networks.models import VLAN

    tree = make_tree()
    with system():
        # The cluster's site is unknown, so a VLAN ID used once in the tenant is clear.
        vlan = VLAN.objects.create(tenant=tree["tenant"], site=tree["site"], vid=10, name="Ten")
        connection = Connection.objects.create(
            created_by=super_admin,
            name="A",
            tenant=tree["tenant"],
            url="https://pve1.lab:8006",
            secret=crypto.encrypt(TOKEN),
            guests=["100", "200"],
            seen=["100", "101", "200"],
        )
        connections.sync(connection, super_admin)
        assert VMInterface.objects.get(vm__external_id="100").vlan == vlan
        assert IPAddress.objects.get(host="10.0.0.53").vm_interface is not None
        answer = pve.answer
        pve.answer = lambda path: (
            {"data": {"net0": "virtio=AA:BB:CC:00:00:01,bridge=vmbr0"}}
            if path.endswith("/qemu/100/config")
            else {"data": {"net0": "name=eth0,bridge=vmbr0,hwaddr=AA:BB:CC:00:00:02,ip=dhcp"}}
            if path.endswith("/lxc/200/config")
            else answer(path)
        )
        connections.sync(connection, super_admin)
        assert VMInterface.objects.get(vm__external_id="100").vlan is None
        assert VMInterface.objects.get(vm__external_id="200").addresses == ""
        assert IPAddress.objects.get(host="10.0.0.53").vm_interface is None
