"""Fill a tenant with a large, realistic inventory for load tests and demos."""

from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.test import RequestFactory

from patchmanager.accounts.models import User
from patchmanager.cabling import services as cabling
from patchmanager.dcim import devices
from patchmanager.dcim.models import Rack
from patchmanager.library import services as library
from patchmanager.locations import services as locations
from patchmanager.locations.models import Building, Floor, Room, Site
from patchmanager.tenancy import rls
from patchmanager.tenancy import services as tenancy

RACKS_PER_ROOM = 50
CABLES_PER_RACK = 24


def seed(actor: User, *, racks: int, slug: str = "scale") -> dict[str, int]:
    """Create ``racks`` racks, each with a 52 port switch, a 48 port panel and 24 cables.

    Everything goes through the services, so permissions, history, audit and path
    tracing do the same work as for people using the application.
    """
    request = RequestFactory().post("/")
    request.user = actor
    with rls.system_context():
        tenant = tenancy.create_tenant(request, actor, slug=slug, name=f"Scale test {slug}")
        vendor = library.get_or_create_vendor(request, actor, "Scale")
        switch = library.create_type(request, actor, tenant=tenant, vendor=vendor, model="S52")
        library.add_port_template(request, actor, switch, name_pattern="ge{n}", count=48)
        library.add_port_template(
            request, actor, switch, name_pattern="te{n}", count=4, connector="sfp+"
        )
        panel = library.create_type(
            request, actor, tenant=tenant, vendor=vendor, model="P24", full_depth=False
        )
        rear = library.add_port_template(
            request, actor, panel, name_pattern="R{n}", kind="rear", count=24
        )
        library.add_port_template(
            request, actor, panel, name_pattern="F{n}", kind="front", count=24, rear_template=rear
        )
        for n in range(racks):
            if n % RACKS_PER_ROOM == 0:
                room = _room(request, actor, tenant, n // RACKS_PER_ROOM + 1)
            _rack(request, actor, room, n, switch, panel)
    return {"racks": racks, "ports": racks * 100, "cables": racks * CABLES_PER_RACK}


def _room(request: Any, actor: User, tenant: Any, number: int) -> Room:
    site = locations.create(request, actor, Site, parent=tenant, name=f"Site {number}")
    building = locations.create(request, actor, Building, parent=site, name="B1")
    floor = locations.create(request, actor, Floor, parent=building, name="F1", level=0)
    return locations.create(request, actor, Room, parent=floor, name="DC")


def _rack(request: Any, actor: User, room: Room, n: int, switch: Any, panel: Any) -> None:
    rack = locations.create(request, actor, Rack, parent=room, name=f"R{n % RACKS_PER_ROOM + 1:02}")
    sw = devices.create(
        request, actor, device_type=switch, room=room, rack=rack, position=40, name=f"sw-{n + 1:04}"
    )
    pp = devices.create(
        request, actor, device_type=panel, room=room, rack=rack, position=42, name=f"pp-{n + 1:04}"
    )
    sw_ports = list(sw.ports.order_by("order")[:CABLES_PER_RACK])
    pp_ports = list(pp.ports.filter(kind="front").order_by("order"))
    for a, b in zip(sw_ports, pp_ports, strict=True):
        cabling.connect(request, actor, a, b)


class Command(BaseCommand):
    help = "Create a tenant with many racks, devices, ports and cables (load tests, demos)."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--actor", required=True, help="email of a super admin")
        parser.add_argument("--racks", type=int, default=500)
        parser.add_argument("--slug", default="scale")

    def handle(self, *args: Any, **options: Any) -> None:
        try:
            actor = User.objects.get(email=options["actor"], is_active=True)
        except User.DoesNotExist as exc:
            raise CommandError("No active user with this email.") from exc
        counts = seed(actor, racks=options["racks"], slug=options["slug"])
        self.stdout.write(", ".join(f"{v} {k}" for k, v in counts.items()))
