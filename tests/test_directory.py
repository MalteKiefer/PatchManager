import json
from typing import Any

import pytest
from django.db import DatabaseError, transaction
from django.test import Client

from patchmanager.circuits.models import Circuit
from patchmanager.directory.models import Contact, Link, Organization, System
from patchmanager.ipam.models import (
    DHCPReservation,
    DHCPScope,
    DNSRecord,
    DNSZone,
    IPAddress,
    Prefix,
)
from tests.conftest import login

pytestmark = pytest.mark.django_db


@pytest.fixture
def setup(admin_client: Client, make_tree):
    tree = make_tree()
    admin_client.post("/tenants/switch/", {"tenant": tree["tenant"].pk}, secure=True)
    return tree


def _post(client: Client, url: str, data: dict[str, Any]) -> Any:
    return client.post(url, data, secure=True)


def test_organizations_contacts_systems_and_links(admin_client: Client, setup, system) -> None:
    created = _post(
        admin_client,
        "/items/organizations/new/",
        {"name": "Acme GmbH", "kind": "customer", "number": "K-100"},
    )
    with system():
        org = Organization.objects.get(name="Acme GmbH")
    assert created.url == f"/items/organizations/{org.pk}/"
    _post(admin_client, "/items/contacts/new/", {"parent": org.pk, "name": "Kim", "role": "IT"})
    _post(
        admin_client,
        "/items/systems/new/",
        {"name": "Phone system", "kind": "telephony", "customer": org.pk, "criticality": "high"},
    )
    with system():
        phone = System.objects.get(name="Phone system")
        assert Contact.objects.get().organization == org and phone.customer == org
    building = setup["building"]
    target = f"locations.building:{building.pk}"
    page = admin_client.get(f"/directory/links/new/?target={target}", secure=True).content.decode()
    assert "Phone system" in page
    _post(
        admin_client,
        "/directory/links/new/",
        {"target": target, "system": phone.pk, "role": "part"},
    )
    _post(
        admin_client,
        "/directory/links/new/",
        {"target": target, "organization": org.pk, "role": "customer"},
    )
    both = _post(
        admin_client,
        "/directory/links/new/",
        {"target": target, "organization": org.pk, "system": phone.pk, "role": "owner"},
    )
    assert "Choose a system or an organization." in both.content.decode()
    node = admin_client.get(f"/locations/building/{building.pk}/", secure=True).content.decode()
    assert "Phone system" in node and "Acme GmbH" in node
    detail = admin_client.get(f"/items/systems/{phone.pk}/", secure=True).content.decode()
    assert building.name in detail
    with system():
        link = Link.objects.get(system=phone)
    _post(admin_client, f"/directory/links/{link.pk}/delete/", {"next": "https://evil.example/"})
    with system():
        assert not Link.objects.filter(system=phone).exists()
    listing = admin_client.get("/items/organizations/", secure=True).content.decode()
    assert "K-100" in listing and "Systems" in listing


def test_links_stay_in_their_tenant(make_tree, super_admin, system) -> None:
    from django.test import RequestFactory

    from patchmanager.accounts.services import ServiceError
    from patchmanager.directory import links

    a, b = make_tree(), make_tree(name="Other")
    request = RequestFactory().post("/")
    request.user = super_admin
    with system():
        foreign = Organization.objects.create(tenant=b["tenant"], name="Foreign")
        with pytest.raises(ServiceError):
            links.add(request, super_admin, a["building"], role="customer", organization=foreign)
        with pytest.raises(DatabaseError), transaction.atomic():
            System.objects.create(tenant=a["tenant"], name="X", customer=foreign)


def test_viewers_cannot_create(client: Client, make_tree, make_user, grant) -> None:
    tree = make_tree()
    viewer = make_user()
    grant(viewer, "viewer", tree["tenant"])
    login(client, viewer)
    client.post("/tenants/switch/", {"tenant": tree["tenant"].pk}, secure=True)
    assert client.get("/items/organizations/", secure=True).status_code == 200
    assert client.get("/items/organizations/new/", secure=True).status_code == 403
    target = f"locations.building:{tree['building'].pk}"
    assert client.get(f"/directory/links/new/?target={target}", secure=True).status_code == 403
    assert (
        client.get("/directory/links/new/?target=accounts.user:1", secure=True).status_code == 404
    )


def test_dhcp_scope_reservations_and_export(admin_client: Client, setup, system) -> None:
    with system():
        prefix = Prefix.objects.create(tenant=setup["tenant"], prefix="10.50.0.0/24")
    bad = _post(
        admin_client,
        "/items/dhcp/new/",
        {
            "name": "Office",
            "prefix": prefix.pk,
            "start": "10.50.1.10",
            "end": "10.50.0.200",
            "lease_seconds": 86400,
            "enabled": "on",
        },
    )
    assert "inside the prefix" in bad.content.decode()
    _post(
        admin_client,
        "/items/dhcp/new/",
        {
            "name": "Office",
            "prefix": prefix.pk,
            "start": "10.50.0.100",
            "end": "10.50.0.200",
            "gateway": "10.50.0.1",
            "dns_servers": "10.50.0.53, 1.1.1.1",
            "domain_name": "office.lan",
            "lease_seconds": 3600,
            "options": "66=tftp.office.lan",
            "enabled": "on",
        },
    )
    with system():
        scope = DHCPScope.objects.get(name="Office")
    _post(
        admin_client,
        "/items/dhcp-reservations/new/",
        {
            "parent": scope.pk,
            "mac_address": "AA-BB-CC-DD-EE-01",
            "address": "10.50.0.20",
            "hostname": "printer-1",
        },
    )
    outside = _post(
        admin_client,
        "/items/dhcp-reservations/new/",
        {"parent": scope.pk, "mac_address": "aa:bb:cc:dd:ee:02", "address": "192.168.1.5"},
    )
    assert "inside the prefix" in outside.content.decode()
    with system():
        assert DHCPReservation.objects.get().mac_address == "aa:bb:cc:dd:ee:01"
    kea = json.loads(admin_client.get(f"/ipam/dhcp/{scope.pk}/kea.json", secure=True).content)
    subnet = kea["subnet4"][0]
    assert subnet["pools"] == [{"pool": "10.50.0.100 - 10.50.0.200"}]
    assert {"code": 66, "data": "tftp.office.lan"} in subnet["option-data"]
    assert subnet["reservations"][0]["hostname"] == "printer-1"


def test_dns_zone_records_and_zone_file(admin_client: Client, setup, system) -> None:
    with system():
        address = IPAddress.objects.create(
            tenant=setup["tenant"],
            address="10.60.0.5/24",
            path=str(setup["tenant"].pk).replace("-", ""),
        )
    _post(
        admin_client, "/items/dns/new/", {"name": "example.lan", "default_ttl": 3600, "serial": 7}
    )
    with system():
        zone = DNSZone.objects.get()
    for data in (
        {"name": "www", "type": "A", "value": "10.60.0.5"},
        {"name": "@", "type": "MX", "value": "mail.example.lan.", "priority": 10},
        {"name": "@", "type": "TXT", "value": 'v=spf1 "mx" -all'},
    ):
        _post(admin_client, "/items/dns-records/new/", {"parent": zone.pk, **data})
    wrong = _post(
        admin_client,
        "/items/dns-records/new/",
        {"parent": zone.pk, "name": "x", "type": "AAAA", "value": "10.0.0.1"},
    )
    assert "AAAA records IPv6" in wrong.content.decode()
    inject = _post(
        admin_client,
        "/items/dns-records/new/",
        {"parent": zone.pk, "name": "y", "type": "CNAME", "value": "a b"},
    )
    assert "Enter a host name." in inject.content.decode()
    with system():
        assert DNSRecord.objects.get(type="A").address == address
    text = admin_client.get(f"/ipam/dns/{zone.pk}/zone.txt", secure=True).content.decode()
    assert "$ORIGIN example.lan." in text and "(7 3600" in text
    assert "www IN A 10.60.0.5" in text and "@ IN MX 10 mail.example.lan." in text
    assert '@ IN TXT "v=spf1 \\"mx\\" -all"' in text


def test_circuits_and_device_warranty(admin_client: Client, setup, make_type, system) -> None:
    _post(admin_client, "/items/organizations/new/", {"name": "Telco", "kind": "provider"})
    with system():
        telco = Organization.objects.get(name="Telco")
    _post(
        admin_client,
        "/items/circuits/new/",
        {
            "circuit_id": "DE-123",
            "provider": telco.pk,
            "kind": "internet",
            "status": "active",
            "site": setup["site"].pk,
            "download_mbps": 1000,
            "contract_end": "2027-12-31",
        },
    )
    with system():
        assert Circuit.objects.get().provider == telco
    page = admin_client.get("/items/circuits/", secure=True).content.decode()
    assert "DE-123" in page and "Telco" in page


def test_site_with_references_does_not_move(make_tree, make_tenant, super_admin, system) -> None:
    from patchmanager.accounts.services import ServiceError
    from patchmanager.locations import services as tree

    t = make_tree()
    with system():
        System.objects.create(tenant=t["tenant"], name="Doors", site=t["site"])
        with pytest.raises(ServiceError, match="refer to this site"):
            tree.move_site(t["request"], super_admin, t["site"], make_tenant())


def test_zone_fields_and_choices_are_safe(admin_client: Client, setup, make_tree, system) -> None:
    from patchmanager.ipam.pages import zone_file

    bad = _post(
        admin_client,
        "/items/dns/new/",
        {"name": "safe.lan", "primary_server": "ns1 IN A 1.2.3.4", "default_ttl": 60, "serial": 1},
    )
    assert "Enter a host name." in bad.content.decode()
    with system():
        zone = DNSZone.objects.create(
            tenant=setup["tenant"],
            name="safe.lan",
            primary_server="ns1.safe.lan\nevil IN A 6.6.6.6",
        )
        assert "\nevil" not in zone_file(zone)
        other = make_tree(name="Elsewhere")
        Organization.objects.create(tenant=other["tenant"], name="Hidden Org")
    page = admin_client.get("/items/systems/new/", secure=True).content.decode()
    assert "Hidden Org" not in page


def test_links_inside_a_site_block_its_move(make_tree, make_tenant, super_admin, system) -> None:
    from django.test import RequestFactory

    from patchmanager.accounts.services import ServiceError
    from patchmanager.directory import links
    from patchmanager.locations import services as tree

    t = make_tree()
    request = RequestFactory().post("/")
    request.user = super_admin
    with system():
        doors = System.objects.create(tenant=t["tenant"], name="Doors")
        links.add(request, super_admin, t["room"], role="part", system=doors)
        with pytest.raises(ServiceError, match="refer to this site"):
            tree.move_site(t["request"], super_admin, t["site"], make_tenant())


def test_webhooks_highlight_administration(admin_client: Client) -> None:
    page = admin_client.get("/integrations/webhooks/", secure=True).content.decode()
    import re

    current = re.findall(r'<a href="([^"]+)" aria-current="page"', page)
    assert "/manage/" in current and "/exchange/" not in current


def test_record_pages_edit_and_delete(admin_client: Client, setup, system) -> None:
    _post(admin_client, "/items/organizations/new/", {"name": "Edit Me", "kind": "supplier"})
    with system():
        org = Organization.objects.get(name="Edit Me")
    _post(admin_client, "/items/contacts/new/", {"parent": org.pk, "name": "Jo"})
    with system():
        contact = Contact.objects.get()
    page = admin_client.get(f"/items/contacts/{contact.pk}/", secure=True).content.decode()
    assert "Edit Me" in page  # breadcrumb back to the organisation
    _post(
        admin_client,
        f"/items/contacts/{contact.pk}/",
        {"action": "edit", "name": "Jo Doe", "role": "", "email": "", "phone": "", "notes": ""},
    )
    with system():
        assert Contact.objects.get().name == "Jo Doe"
    deleted = _post(admin_client, f"/items/contacts/{contact.pk}/", {"action": "delete"})
    assert deleted.url == f"/items/organizations/{org.pk}/"
    _post(
        admin_client,
        f"/items/organizations/{org.pk}/",
        {"action": "edit", "name": "Edited", "kind": "supplier"},
    )
    with system():
        assert Organization.objects.get(pk=org.pk).name == "Edited"
    _post(
        admin_client,
        "/items/circuits/new/",
        {"circuit_id": "L1", "provider": org.pk, "kind": "dsl", "status": "active"},
    )
    blocked = admin_client.post(
        f"/items/organizations/{org.pk}/", {"action": "delete"}, secure=True, follow=True
    )
    assert "still use" in blocked.content.decode()
    assert admin_client.get("/items/nothing/", secure=True).status_code == 404
    assert admin_client.get("/items/contacts/", secure=True).status_code == 404  # no own list
