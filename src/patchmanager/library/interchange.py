"""Import and export device types in the community device type library format.

The format (YAML or JSON) lists ``manufacturer``, ``model``, ``u_height``,
``is_full_depth`` and ports under ``interfaces``, ``front-ports``,
``rear-ports``, ``console-ports`` and ``power-ports``. Uploaded files and the
bundled catalog (``library.catalog``) both go through ``import_type``.
"""

import re
from collections.abc import Iterable
from decimal import Decimal
from typing import Any

import yaml
from django.db import transaction
from django.http import HttpRequest
from django.utils.translation import gettext as _

from patchmanager.accounts.models import User
from patchmanager.accounts.services import ServiceError
from patchmanager.library import services
from patchmanager.library.models import (
    Airflow,
    Connector,
    DeviceType,
    Mounting,
    PortKind,
    PortTemplate,
    expand,
)
from patchmanager.tenancy.models import Tenant

# interface type -> (connector, speed in Mbit/s)
_INTERFACES: dict[str, tuple[str, int | None]] = {
    "100base-tx": (Connector.RJ45, 100),
    "1000base-t": (Connector.RJ45, 1000),
    "2.5gbase-t": (Connector.RJ45, 2500),
    "5gbase-t": (Connector.RJ45, 5000),
    "10gbase-t": (Connector.RJ45, 10000),
    "1000base-x-sfp": (Connector.SFP, 1000),
    "10gbase-x-sfpp": (Connector.SFP_PLUS, 10000),
    "25gbase-x-sfp28": (Connector.SFP28, 25000),
    "40gbase-x-qsfpp": (Connector.QSFP, 40000),
    "100gbase-x-qsfp28": (Connector.QSFP28, 100000),
}
_CONNECTORS = [
    (r"^8p8c|^rj-?45|^cat", Connector.RJ45),
    (r"^rj-?11|^6p", Connector.RJ11),
    (r"^lc", Connector.LC),
    (r"^sc", Connector.SC),
    (r"^st", Connector.ST),
    (r"^mpo|^mtp", Connector.MPO),
    (r"^e2000", Connector.E2000),
    (r"usb-?c", Connector.USB_C),
    (r"usb", Connector.USB),
    (r"hdmi", Connector.HDMI),
    (r"mini-?(dp|displayport)", Connector.MINI_DP),
    (r"^dp$|displayport", Connector.DISPLAYPORT),
    (r"dvi", Connector.DVI),
    (r"vga|de-?15", Connector.VGA),
    (r"db-?9|de-?9", Connector.SERIAL),
    (r"c13$", Connector.C13),
    (r"c14$", Connector.C14),
    (r"c19$", Connector.C19),
    (r"c20$", Connector.C20),
    (r"cee-7|schuko", Connector.SCHUKO),
    (r"^(f|bnc)$|coax", Connector.COAX),
]
_SECTIONS = {
    "interfaces": PortKind.INTERFACE,
    "front-ports": PortKind.FRONT,
    "rear-ports": PortKind.REAR,
    "console-ports": PortKind.CONSOLE,
    "power-ports": PortKind.POWER,
}
_TRAILING_NUMBER = re.compile(r"^(.*?)(\d+)$")


_CAGES = [
    (r"qsfp28|qsfp56|qsfp-?dd|osfp|qsfp112", Connector.QSFP28),
    (r"qsfpp|qsfp\+|qsfp", Connector.QSFP),
    (r"sfp28|sfp56|sfp-?dd", Connector.SFP28),
    (r"sfpp|sfp\+|xfp", Connector.SFP_PLUS),
    (r"sfp|-x$|base-x", Connector.SFP),
    (r"base-t|base-tx", Connector.RJ45),
]
_SPEED = re.compile(r"^(\d+(?:\.\d+)?)(g|m)base")
NOT_PHYSICAL = frozenset({"virtual", "lag", "bridge"})


def _connector(kind: str, port_type: str) -> tuple[str, int | None]:
    port_type = port_type.lower()
    if kind == PortKind.INTERFACE:
        if port_type.startswith("ieee802.11"):
            return Connector.WIRELESS, None
        if port_type in _INTERFACES:
            return _INTERFACES[port_type]
        speed = _SPEED.match(port_type)
        mbps = (
            int(float(speed.group(1)) * (1000 if speed.group(2) == "g" else 1)) if speed else None
        )
        for pattern, connector in _CAGES:
            if re.search(pattern, port_type):
                return connector, mbps
    for pattern, connector in _CONNECTORS:
        if re.search(pattern, port_type):
            return connector, None
    return Connector.OTHER, None


def _runs(names: list[str]) -> Iterable[tuple[str, int, int]]:
    """Group names like ``Gi1/0/1 .. Gi1/0/48`` into ``(pattern, start, count)``."""
    index = 0
    while index < len(names):
        match = _TRAILING_NUMBER.match(names[index])
        if not match or (match.group(2).startswith("0") and len(match.group(2)) > 1):
            yield names[index].replace("{", "").replace("}", ""), 1, 1
            index += 1
            continue
        prefix, number = match.group(1), int(match.group(2))
        count = 1
        while index + count < len(names) and names[index + count] == f"{prefix}{number + count}":
            count += 1
        safe = prefix.replace("{", "").replace("}", "")
        yield (
            (f"{safe}{{n}}" if count > 1 else names[index].replace("{", "").replace("}", "")),
            number,
            count,
        )
        index += count


def _ports(data: dict[str, Any], section: str) -> list[dict[str, Any]]:
    items = data.get(section) or []
    if not isinstance(items, list) or not all(
        isinstance(i, dict) and isinstance(i.get("name"), str) for i in items
    ):
        raise ServiceError(
            _("%(section)s must be a list of ports with names.") % {"section": section}
        )
    if len(items) > 1024:
        raise ServiceError(_("Too many ports."))
    return items


def import_type(
    request: HttpRequest, actor: User, payload: str, *, tenant: Tenant | None
) -> DeviceType:
    """Create a device type from one YAML or JSON document."""
    try:
        data = yaml.safe_load(payload)
    except yaml.YAMLError as exc:
        raise ServiceError(_("The file is not valid YAML or JSON.")) from exc
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("manufacturer"), str)
        or not isinstance(data.get("model"), str)
    ):
        raise ServiceError(_("The file needs a manufacturer and a model."))
    with transaction.atomic():
        vendor = services.get_or_create_vendor(request, actor, data["manufacturer"][:100])
        u_height = data.get("u_height", 1)
        is_floor = not isinstance(u_height, int | float) or u_height < 1
        device_type = services.create_type(
            request,
            actor,
            tenant=tenant,
            vendor=vendor,
            model=data["model"][:150],
            form=DeviceType.Form.FLOOR if is_floor else DeviceType.Form.RACK,
            u_height=0 if is_floor else int(u_height),
            full_depth=bool(data.get("is_full_depth", True)),
            part_number=str(data.get("part_number", ""))[:100],
            **_details(data, is_floor),
            description=str(data.get("comments", ""))[:2000],
        )
        _rename_rears(data)
        rear_by_name: dict[str, PortTemplate] = {}
        order = 0
        for section in ("rear-ports", "interfaces", "front-ports", "console-ports", "power-ports"):
            kind = _SECTIONS[section]
            items = _ports(data, section)
            if kind == PortKind.FRONT:
                order = _import_front(device_type, items, rear_by_name, order)
                continue
            groups: dict[tuple[str, int], list[str]] = {}
            for item in items:
                if kind == PortKind.INTERFACE and str(item.get("type", "")) in NOT_PHYSICAL:
                    continue  # logical interfaces (LAG, VLAN) are not ports
                key = (str(item.get("type", "")), int(item.get("positions", 1) or 1))
                groups.setdefault(key, []).append(item["name"][:100])
            for (port_type, positions), names in groups.items():
                connector, speed = _connector(kind, port_type)
                for pattern, start, count in _runs(names):
                    template = PortTemplate.objects.create(
                        device_type=device_type,
                        name_pattern=pattern,
                        start=start,
                        count=count,
                        kind=kind,
                        connector=connector,
                        speed_mbps=speed,
                        positions=positions,
                        order=order,
                    )
                    order += 1
                    if kind == PortKind.REAR:
                        for name in template.names():
                            rear_by_name[name] = template
    return device_type


_WEIGHT_UNITS = {"kg": 1.0, "g": 0.001, "lb": 0.45359237, "oz": 0.028349523}


def _details(data: dict[str, Any], is_floor: bool) -> dict[str, Any]:
    """Mounting, airflow, power supplies, power and weight, where the file has them."""
    details: dict[str, Any] = {"mounting": "" if is_floor else Mounting.RACK_19}
    if data.get("airflow") in Airflow.values:
        details["airflow"] = data["airflow"]
    supplies = [p for p in data.get("power-ports") or [] if isinstance(p, dict)]
    if supplies:
        details["power_supplies"] = min(len(supplies), 16)
        draws = [p["maximum_draw"] for p in supplies if isinstance(p.get("maximum_draw"), int)]
        if draws and 0 < max(draws) <= 100_000:
            details["max_power_w"] = max(draws)  # redundant supplies: one carries the load
    weight, unit = data.get("weight"), _WEIGHT_UNITS.get(str(data.get("weight_unit", "kg")))
    if isinstance(weight, int | float) and unit and 0 < weight * unit < 100_000:
        details["weight_kg"] = Decimal(str(round(weight * unit, 2)))
    return details


def _rename_rears(data: dict[str, Any]) -> None:
    """Rear ports named like front ports ("1" and "1") get an R: names are unique per device."""
    fronts = {str(p.get("name", "")).lower() for p in data.get("front-ports") or []}
    renamed = {}
    for port in data.get("rear-ports") or []:
        name = str(port.get("name", ""))
        if name.lower() in fronts:
            renamed[name] = f"R{name}" if name.isdigit() else f"{name} rear"
            port["name"] = renamed[name]
    for port in data.get("front-ports") or []:
        rear = str(port.get("rear_port", ""))
        if rear in renamed:
            port["rear_port"] = renamed[rear]


def _import_front(
    device_type: DeviceType, items: list[dict[str, Any]], rears: dict[str, PortTemplate], order: int
) -> int:
    """Front ports keep their explicit rear mapping by creating one template per front port."""
    for item in items:
        connector = _connector(PortKind.FRONT, str(item.get("type", "")))[0]
        rear_name = str(item.get("rear_port", ""))[:100]
        position = item.get("rear_port_position", 1)
        PortTemplate.objects.create(
            device_type=device_type,
            name_pattern=item["name"][:100].replace("{", "").replace("}", ""),
            kind=PortKind.FRONT,
            connector=connector,
            rear_template=rears.get(rear_name),
            rear_port_name=rear_name if rear_name in rears else "",
            rear_position=int(position) if isinstance(position, int) and position > 0 else 1,
            order=order,
        )
        order += 1
    return order


_EXPORT_TYPES = {v: k for k, v in _INTERFACES.items()}


def export_type(device_type: DeviceType, *, fmt: str = "yaml") -> str:
    """Serialise a device type in the community format."""
    data: dict[str, Any] = {
        "manufacturer": device_type.vendor.name,
        "model": device_type.model,
        "slug": re.sub(
            r"[^a-z0-9]+", "-", f"{device_type.vendor.name}-{device_type.model}".lower()
        ).strip("-"),
        "part_number": device_type.part_number,
        "u_height": device_type.u_height,
        "is_full_depth": device_type.full_depth,
    }
    if device_type.airflow:
        data["airflow"] = device_type.airflow
    if device_type.weight_kg is not None:
        data["weight"], data["weight_unit"] = float(device_type.weight_kg), "kg"
    sections: dict[str, list[dict[str, Any]]] = {}
    rear_names: dict[Any, list[str]] = {}
    for template in device_type.port_templates.all():
        if template.kind == PortKind.REAR:
            rear_names[template.pk] = template.names()
    for template in device_type.port_templates.all():
        section = (
            next(k for k, v in _SECTIONS.items() if v == template.kind)
            if template.kind in _SECTIONS.values()
            else "interfaces"
        )
        for index, name in enumerate(template.names()):
            entry: dict[str, Any] = {
                "name": name,
                "type": _EXPORT_TYPES.get(
                    (template.connector, template.speed_mbps), template.connector
                ),
            }
            if template.kind == PortKind.REAR:
                entry["positions"] = template.positions
            if template.kind == PortKind.FRONT and template.rear_port_name:
                entry["rear_port"] = template.rear_port_name
                entry["rear_port_position"] = template.rear_position or 1
            elif template.kind == PortKind.FRONT and template.rear_template_id in rear_names:
                rears = rear_names[template.rear_template_id]
                per_rear = max(1, -(-template.count // len(rears)))
                entry["rear_port"] = rears[min(index // per_rear, len(rears) - 1)]
                entry["rear_port_position"] = index % per_rear + 1
            sections.setdefault(section, []).append(entry)
    data.update(sections)
    if fmt == "json":
        import json

        return json.dumps(data, indent=2)
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)


__all__ = ["expand", "export_type", "import_type"]
