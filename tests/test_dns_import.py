import pytest
from django.test import Client

from patchmanager.accounts.services import ServiceError
from patchmanager.audit.models import AuditEvent
from patchmanager.ipam import dns_import
from patchmanager.ipam.models import DNSRecord, DNSZone

pytestmark = pytest.mark.django_db

ZONE = """$ORIGIN example.com.
$TTL 3600
@   IN  SOA ns1.example.com. hostmaster.example.com. (
            2026102201 ; serial
            7200 900 1209600 300 )
    IN  NS  ns1.example.com.
    IN  MX  10 mail.example.com.
www 300 IN A 192.0.2.10
    IN AAAA 2001:db8::10
mail    A   192.0.2.20
ftp     CNAME www
txt     TXT "v=spf1 " "include:spf.example.net -all"   ; two strings
_sip._tcp SRV 10 60 5060 sip.example.com.
old.example.com. A 192.0.2.30
other.example.net. A 192.0.2.40
dnssec  DNSKEY 257 3 13 abc
"""


def test_zone_file() -> None:
    zone = dns_import.parse_zone_file(ZONE, "example.com")
    assert (zone.primary_server, zone.admin_email, zone.serial, zone.default_ttl) == (
        "ns1.example.com",
        "hostmaster@example.com",
        2026102201,
        3600,
    )
    rows = {(r.name, r.type): r for r in zone.records}
    assert rows[("@", "MX")].priority == 10 and rows[("@", "MX")].value == "mail.example.com."
    assert rows[("www", "A")].ttl == 300 and rows[("www", "AAAA")].value == "2001:db8::10"
    assert rows[("txt", "TXT")].value == "v=spf1 include:spf.example.net -all"
    assert rows[("_sip._tcp", "SRV")].value == "60 5060 sip.example.com."
    assert ("old", "A") in rows and ("other.example.net.", "A") in rows
    assert any("DNSKEY" in s for s in zone.skipped)


def test_import_page_from_file(admin_client: Client, make_tree, system) -> None:
    tree = make_tree()
    admin_client.post("/tenants/switch/", {"tenant": tree["tenant"].pk}, secure=True)
    listing = admin_client.get("/items/dns/", secure=True).content.decode()
    assert "/ipam/dns/import/" in listing
    response = admin_client.post(
        "/ipam/dns/import/",
        {"name": "example.com", "source": "file", "content": ZONE, "token": ""},
        secure=True,
    )
    with system():
        zone = DNSZone.objects.get()
        assert response.url == f"/items/dns/{zone.pk}/"
        assert (
            zone.serial == 2026102201 and zone.records.count() == 9
        )  # other.example.net is outside
        assert DNSRecord.objects.get(type="SRV").priority == 10
    again = admin_client.post(
        "/ipam/dns/import/",
        {"name": "example.com", "source": "file", "content": ZONE, "replace": "on"},
        secure=True,
    )
    assert again.status_code == 302
    with system():
        assert DNSRecord.objects.count() == 9
    empty = admin_client.post("/ipam/dns/import/", {"name": "x.com", "source": "file"}, secure=True)
    assert "Paste a zone file" in empty.content.decode()


@pytest.mark.parametrize("provider", ["desec", "cloudflare", "hetzner"])
def test_providers(admin_client: Client, make_tree, system, monkeypatch, provider) -> None:
    calls = []

    def fake(url, headers):
        calls.append((url, headers))
        if "desec.io" in url:
            return [
                {"subname": "", "type": "MX", "ttl": 3600, "records": ["10 mail.example.org."]},
                {"subname": "www", "type": "A", "ttl": 300, "records": ["192.0.2.1"]},
                {"subname": "", "type": "TXT", "ttl": 300, "records": ['"v=spf1 -all"']},
            ]
        if "cloudflare" in url and "dns_records" not in url:
            return {"result": [{"id": "abc123"}]}
        if "cloudflare" in url:
            return {
                "result": [
                    {"name": "www.example.org", "type": "A", "content": "192.0.2.1", "ttl": 1},
                    {
                        "name": "example.org",
                        "type": "MX",
                        "content": "mail.example.org",
                        "ttl": 300,
                        "priority": 10,
                    },
                    {"name": "example.org", "type": "TXT", "content": "v=spf1 -all", "ttl": 300},
                ],
                "result_info": {"total_pages": 1},
            }
        if "zones?name" in url:
            return {"zones": [{"id": "Z1"}]}
        return {
            "records": [
                {"name": "www", "type": "A", "value": "192.0.2.1", "ttl": 300},
                {"name": "@", "type": "MX", "value": "10 mail.example.org."},
                {"name": "@", "type": "TXT", "value": '"v=spf1 -all"'},
            ]
        }

    monkeypatch.setattr(dns_import, "_get", fake)
    tree = make_tree()
    admin_client.post("/tenants/switch/", {"tenant": tree["tenant"].pk}, secure=True)
    admin_client.post(
        "/ipam/dns/import/",
        {"name": "example.org", "source": provider, "token": "tok-123"},
        secure=True,
    )
    with system():
        records = {(r.name, r.type): r for r in DNSRecord.objects.all()}
        assert records[("www", "A")].value == "192.0.2.1"
        assert (
            records[("@", "MX")].value == "mail.example.org."
            and records[("@", "MX")].priority == 10
        )
        assert records[("@", "TXT")].value == "v=spf1 -all"
        assert all("tok-123" not in str(e.data) for e in AuditEvent.objects.all())
    assert all(
        url.startswith(
            ("https://desec.io/", "https://api.cloudflare.com/", "https://dns.hetzner.com/")
        )
        for url, _headers in calls
    )


def test_provider_input_is_checked() -> None:
    with pytest.raises(ServiceError):
        dns_import.fetch("desec", "../../evil", "t")
    with pytest.raises(ServiceError):
        dns_import.fetch("desec", "example.com", "a\nb")
    with pytest.raises(ServiceError):
        dns_import.fetch("nowhere", "example.com", "t")


def test_replace_needs_delete_permission(make_tree, super_admin, system, monkeypatch) -> None:
    from django.test import RequestFactory

    from patchmanager.accounts import policy
    from patchmanager.accounts.services import PermissionDeniedError

    real = policy.has_perm

    def no_delete(user, permission, obj=None, **kwargs):
        return permission != "ipam.dnszone.delete" and real(user, permission, obj, **kwargs)

    monkeypatch.setattr(policy, "has_perm", no_delete)
    tree = make_tree()
    request = RequestFactory().post("/")
    request.user = super_admin
    with system():
        DNSZone.objects.create(tenant=tree["tenant"], name="example.com")
        zone = dns_import.parse_zone_file(ZONE, "example.com")
        with pytest.raises(PermissionDeniedError):
            dns_import.save(request, super_admin, tree["tenant"], zone, replace=True, source="file")
        result = dns_import.save(
            request, super_admin, tree["tenant"], zone, replace=False, source="file"
        )
        assert result.records == 9
