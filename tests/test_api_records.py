"""API endpoints of the records with shared pages and the administration lists."""

from typing import Any

import pytest

from patchmanager.audit.models import AuditEvent
from patchmanager.directory.models import Organization
from patchmanager.tenancy import rls
from tests.test_api import _api, _get, _send, _token

pytestmark = pytest.mark.django_db

READ_ONLY = ("categories", "room-types")
RECORDS = (
    "circuits",
    "clusters",
    "vms",
    "storage",
    "organizations",
    "systems",
    "dhcp-scopes",
    "dns-zones",
)


@pytest.fixture
def tree(make_tree):
    return make_tree()


def _org(api, tenant, name="Telco") -> dict[str, Any]:
    response = _send(
        api,
        "post",
        "/api/v1/organizations/",
        {"tenant": str(tenant.pk), "name": name, "kind": "provider"},
    )
    assert response.status_code == 201, response.content
    body: dict[str, Any] = response.json()
    return body


def test_records_through_services(tree, make_tree, super_admin) -> None:
    api = _api(_token(super_admin))
    tenant = str(tree["tenant"].pk)
    org = _org(api, tree["tenant"])
    line = _send(
        api,
        "post",
        "/api/v1/circuits/",
        {
            "tenant": tenant,
            "circuit_id": "L-1",
            "provider": org["id"],
            "site": str(tree["site"].pk),
            "download_mbps": 100,
        },
    )
    assert line.status_code == 201, line.content
    line = line.json()
    assert (line["provider"], line["status"]) == (org["id"], "active")
    changed = _send(api, "patch", f"/api/v1/circuits/{line['id']}/", {"status": "cancelled"})
    assert changed.status_code == 200, changed.content
    assert (changed.json()["status"], changed.json()["download_mbps"]) == ("cancelled", 100)
    assert _get(api, "/api/v1/circuits/", q="l-1", status="cancelled").json()["count"] == 1

    # References must belong to the record's tenant, as in the form.
    elsewhere = _org(api, make_tree(name="Other")["tenant"], name="Foreign")
    wrong = _send(api, "patch", f"/api/v1/circuits/{line['id']}/", {"provider": elsewhere["id"]})
    assert wrong.status_code == 400 and "provider" in wrong.json()
    assert _send(api, "post", "/api/v1/circuits/", [1]).status_code == 400

    in_use = _send(api, "delete", f"/api/v1/organizations/{org['id']}/", None)
    assert in_use.status_code == 400
    assert _send(api, "delete", f"/api/v1/circuits/{line['id']}/", None).status_code == 204
    with rls.system_context():  # the audit names the deleted record
        assert AuditEvent.objects.filter(
            action="circuits.circuit.deleted", object_id=line["id"]
        ).exists()
    assert _send(api, "delete", f"/api/v1/organizations/{org['id']}/", None).status_code == 204

    zone = _send(api, "post", "/api/v1/dns-zones/", {"tenant": tenant, "name": "example.org"})
    assert zone.status_code == 201, zone.content
    for key in (*RECORDS, *READ_ONLY):
        assert _get(api, f"/api/v1/{key}/").status_code == 200, key
    assert _get(api, "/api/v1/categories/").json()["count"] > 0
    assert _send(api, "post", "/api/v1/categories/", {"key": "x"}).status_code == 405


def test_limited_tokens_see_nothing_outside_their_limits(tree, make_tree, super_admin) -> None:
    api = _api(_token(super_admin))
    _org(api, tree["tenant"])
    other = make_tree(name="Other")
    sites_only = _api(_token(super_admin, ("read",), object_types=["locations.site"]))
    for key in (*RECORDS, *READ_ONLY):
        assert _get(sites_only, f"/api/v1/{key}/").json()["results"] == [], key
    orgs_only = _api(_token(super_admin, ("read",), object_types=["directory.organization"]))
    assert [o["name"] for o in _get(orgs_only, "/api/v1/organizations/").json()["results"]] == [
        "Telco"
    ]
    other_tenant = _api(_token(super_admin, ("read",), tenant=other["tenant"]))
    assert _get(other_tenant, "/api/v1/organizations/").json()["results"] == []


def test_viewer_cannot_write(tree, make_user, grant, super_admin) -> None:
    org = _org(_api(_token(super_admin)), tree["tenant"])
    viewer = make_user()
    with rls.system_context():
        grant(viewer, "viewer", tree["tenant"])
    api = _api(_token(viewer))
    assert _get(api, f"/api/v1/organizations/{org['id']}/").status_code == 200
    # Lists every member reads in the pages are readable with a role inside a tenant.
    assert _get(api, "/api/v1/categories/").json()["count"] > 0
    url = "/api/v1/organizations/"
    body = {"tenant": str(tree["tenant"].pk), "name": "New"}
    assert _send(api, "post", url, body).status_code == 403
    assert _send(api, "patch", f"{url}{org['id']}/", {"name": "Renamed"}).status_code == 403
    assert _send(api, "delete", f"{url}{org['id']}/", None).status_code == 403
    with rls.system_context():
        assert Organization.objects.get(pk=org["id"]).name == "Telco"
