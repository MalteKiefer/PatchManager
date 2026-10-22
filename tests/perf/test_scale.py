"""Response times and query counts with a large inventory (500 racks, 50 000 ports).

Run with ``pixi run -e dev perf``. ``PERF_RACKS`` scales the inventory down for a quick
local check; the budgets are meant for the full size.
"""

import os
import time

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from patchmanager.api import tokens
from patchmanager.dcim.management.commands.seed_scale import seed
from patchmanager.dcim.models import Device, Port, Rack
from patchmanager.locations.models import Room
from patchmanager.tenancy import rls

pytestmark = [pytest.mark.perf, pytest.mark.django_db]

RACKS = int(os.environ.get("PERF_RACKS", "500"))
SECONDS = 1.5  # per page, measured on CI runners
QUERIES = 60  # a page must not grow with the inventory (no N+1)


def _measure(client, url: str, **extra) -> tuple[float, int]:
    with CaptureQueriesContext(connection) as queries:
        started = time.perf_counter()
        response = client.get(url, secure=True, **extra)
        took = time.perf_counter() - started
    assert response.status_code == 200, (url, response.status_code)
    return took, len(queries)


def test_pages_stay_fast_with_a_large_inventory(admin_client, super_admin) -> None:
    from django.test import Client, RequestFactory

    counts = seed(super_admin, racks=RACKS)
    with connection.cursor() as cursor:
        cursor.execute("ANALYZE")  # what autovacuum does after a bulk load in production
    with rls.system_context():
        assert Port.objects.count() >= counts["ports"]
        room = Room.objects.filter(tenant__slug="scale").first()
        rack = Rack.objects.filter(room=room).first()
        switch = Device.objects.get(name=f"sw-{RACKS // 2:04}")
        port = switch.ports.order_by("order").first()
        request = RequestFactory().post("/")
        request.user = super_admin
        token = tokens.create_token(request, super_admin, name="perf", scopes=["read"], days=1)[1]
    api = Client(HTTP_AUTHORIZATION=f"Bearer {token}")
    pages = {
        "/": admin_client,
        "/locations/": admin_client,
        f"/locations/room/{room.pk}/": admin_client,
        f"/racks/{rack.pk}/": admin_client,
        f"/racks/{rack.pk}/?face=rear": admin_client,
        f"/locations/room/{room.pk}/cabling/": admin_client,
        f"/devices/{switch.pk}/": admin_client,
        f"/devices/ports/{port.pk}/": admin_client,
        f"/cabling/trace/{port.pk}/": admin_client,
        "/cabling/": admin_client,
        f"/search/?q=sw-{RACKS // 2:04}": admin_client,
        "/history/": admin_client,
        "/api/v1/racks/": api,
        "/api/v1/devices/": api,
        "/api/v1/ports/": api,
        "/api/v1/cables/": api,
        f"/api/v1/search/?tenant={room.tenant_id}&q=sw-{RACKS // 2:04}": api,
    }
    results = {url: _measure(client, url) for url, client in pages.items()}
    report = "\n".join(
        f"{t * 1000:7.0f} ms {q:4} queries  {url}" for url, (t, q) in results.items()
    )
    print(f"\n{counts}\n{report}")
    slow = {
        url: (took, count)
        for url, (took, count) in results.items()
        if took > SECONDS or count > QUERIES
    }
    assert not slow, report
