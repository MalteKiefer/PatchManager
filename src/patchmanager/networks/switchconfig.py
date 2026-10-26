"""Take a switch configuration into the documentation of a device, and back out.

:func:`apply` compares a parsed configuration with what is documented and writes
the differences. With ``commit=False`` it runs the same work in a transaction that
is rolled back, so the preview lists exactly what applying would change.
"""

import ipaddress
from dataclasses import dataclass, field
from typing import Any

from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import HttpRequest
from django.utils.translation import gettext as _

from patchmanager.accounts import policy
from patchmanager.accounts.models import User
from patchmanager.accounts.services import ServiceError
from patchmanager.accounts.services.users import require
from patchmanager.audit import services as audit
from patchmanager.dcim.models import Device, Port
from patchmanager.ipam import services as ipam
from patchmanager.ipam.models import IPAddress
from patchmanager.library.models import Connector, PortKind
from patchmanager.networks.models import (
    VLAN,
    BGPNeighbor,
    MSTInstance,
    PortProfile,
    StaticRoute,
    SwitchSettings,
    TaggedVLAN,
)
from patchmanager.networks.platforms import common as model


@dataclass
class Result:
    """What applying changes (or would change), and what was left out."""

    changes: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    def add(self, text: str) -> None:
        """Record one change."""
        self.changes.append(text)


class _Rollback(Exception):  # noqa: N818  # control flow, not an error
    """Leave the preview transaction without keeping anything."""


def apply(
    request: HttpRequest,
    actor: User,
    device: Device,
    config: model.Config,
    *,
    commit: bool,
    source: str = "",
) -> Result:
    """Make the documentation of ``device`` match ``config`` (or show what that would do)."""
    require(actor, "dcim.device.change", device)
    require(actor, "dcim.device.import", device)  # reading a configuration is an import
    result = Result(skipped=[*config.warnings])
    if config.secrets_skipped:
        result.skipped.append(
            _("%(n)s lines with secrets were not read.") % {"n": config.secrets_skipped}
        )
    if config.unknown:
        result.skipped.append(
            _("%(n)s commands are not documented, for example: %(first)s")
            % {"n": len(config.unknown), "first": config.unknown[0]}
        )
    try:
        with transaction.atomic():
            _apply(request, actor, device, config, result, source)
            if not commit:
                raise _Rollback
    except _Rollback:
        return result
    audit.record(
        "networks.config.applied",
        request=request,
        obj=device,
        data={"changes": len(result.changes), "source": source[:200]},
    )
    return result


def _apply(
    request: HttpRequest,
    actor: User,
    device: Device,
    config: model.Config,
    result: Result,
    source: str,
) -> None:
    # Names are compared in their canonical form: Gi1/0/1 is GigabitEthernet1/0/1.
    ports = {model.canonical(p.name): p for p in Port.objects.filter(device=device)}

    def port(name: str) -> Port | None:
        found = ports.get(model.canonical(name))
        if found is None and f"skip:{name}" not in ports:
            result.skipped.append(_("Port %(port)s is not on this device.") % {"port": name})
            ports[f"skip:{name}"] = None  # type: ignore[assignment]  # report once
        return found

    _settings(device, config, result, source)
    for trunk in config.trunks.values():
        lag = ports.get(model.canonical(trunk.name))
        if lag is not None and lag.kind != PortKind.LAG:  # documented as a plain port
            result.skipped.append(
                _("%(port)s is documented as a port, not as a link aggregation.")
                % {"port": lag.name}
            )
            continue
        if lag is None:
            lag = Port.objects.create(
                device=device,
                name=trunk.name,
                kind=PortKind.LAG,
                connector=Connector.OTHER,
                order=10_000,
            )
            ports[model.canonical(trunk.name)] = lag
            result.add(_("New link aggregation %(lag)s") % {"lag": trunk.name})
        if lag.lag_protocol != trunk.protocol:
            lag.lag_protocol = trunk.protocol
            lag.save(update_fields=["lag_protocol", "updated_at"])
        for name in trunk.members:
            member = port(name)
            if member is not None and member.lag_id != lag.pk:
                member.lag = lag
                member.save(update_fields=["lag", "updated_at"])
                result.add(_("%(port)s joins %(lag)s") % {"port": member.name, "lag": lag.name})
    for item in config.interfaces.values():
        target = port(item.name)
        if target is not None:
            _port_settings(target, item, result)
    vlans = {
        vid: _vlan(request, actor, device, vid, v.name, result) for vid, v in config.vlans.items()
    }
    _memberships(config, vlans, port, ports, result)
    for vlan_item in config.vlans.values():
        if vlan_item.addresses or vlan_item.dhcp or vlan_item.ospf_area:
            _vlan_interface(request, actor, device, vlan_item, ports, result)
    for item in config.interfaces.values():
        target = port(item.name) if item.addresses else None
        if target is not None:
            _addresses(request, actor, device, target, item.addresses, result)
    _replace(device, config, result)
    _dhcp(request, actor, device, config, result)


def _settings(device: Device, config: model.Config, result: Result, source: str) -> None:
    settings, _created = SwitchSettings.objects.get_or_create(
        device=device, defaults={"tenant_id": device.tenant_id}
    )
    wanted = {
        "hostname": config.hostname,
        "stp_mode": config.stp_mode,
        "stp_priority": config.stp_priority,
        "mst_region": config.mst_region,
        "mst_revision": config.mst_revision,
        "ip_routing": config.ip_routing,
        "bgp_asn": config.bgp_asn,
        "bgp_router_id": config.bgp_router_id or None,
        "ospf_router_id": config.ospf_router_id or None,
        "gateways": config.gateways,
        "jumbo_vlans": sorted(v.vid for v in config.vlans.values() if v.jumbo),
        "dns_servers": config.dns_servers,
        "domain": config.domain,
    }
    for name, value in wanted.items():
        current = getattr(settings, name)
        if (str(current) if current is not None else None) != (
            str(value) if value is not None else None
        ):
            setattr(settings, name, value)
            label = getattr(SwitchSettings._meta.get_field(name), "verbose_name", name)
            result.add(_("%(field)s: %(value)s") % {"field": label, "value": value or "-"})
    settings.source = source[:200]
    try:
        settings.full_clean(exclude=["tenant", "device"])
    except ValidationError as exc:
        raise ServiceError("; ".join(exc.messages)) from exc
    settings.save()


def _port_settings(target: Port, item: model.Interface, result: Result) -> None:
    wanted: dict[str, Any] = {
        "enabled": item.enabled,
        "tunneled": item.tunneled,
        "stp_edge": item.edge,
        "stp_bpdu_guard": item.bpdu_guard,
        "stp_root_guard": item.root_guard,
    }
    if item.label:
        wanted["label"] = item.label
    changed = [k for k, v in wanted.items() if getattr(target, k) != v]
    if changed:
        for key in changed:
            setattr(target, key, wanted[key])
        target.save(update_fields=[*changed, "updated_at"])
        result.add(
            _("Port %(port)s: %(fields)s")
            % {
                "port": target.name,
                "fields": ", ".join(
                    str(getattr(Port._meta.get_field(k), "verbose_name", k)) for k in changed
                ),
            }
        )


def _vlan(
    request: HttpRequest, actor: User, device: Device, vid: int, name: str, result: Result
) -> VLAN:
    """The documented VLAN with this ID for the device's site (created when missing)."""
    site = device.room.floor.building.site
    vlan = (
        VLAN.objects.filter(tenant_id=device.tenant_id, vid=vid, site=site).first()
        or VLAN.objects.filter(tenant_id=device.tenant_id, vid=vid, site__isnull=True).first()
    )
    if vlan is None:
        from patchmanager.networks import services

        vlan = services.create_vlan(
            request, actor, device.tenant, site=site, vid=vid, name=name or f"VLAN{vid}"
        )
        result.add(
            _("New VLAN %(vid)s %(name)s at %(site)s")
            % {"vid": vid, "name": vlan.name, "site": site}
        )
    elif name and vlan.name != name:
        result.skipped.append(
            _(
                "VLAN %(vid)s is called %(doc)s here and %(cfg)s in the configuration;"
                " the name stays."
            )
            % {"vid": vid, "doc": vlan.name, "cfg": name}
        )
    return vlan


def _memberships(
    config: model.Config, vlans: dict[int, VLAN], port: Any, ports: dict[str, Port], result: Result
) -> None:
    untagged: dict[str, int] = {}
    tagged: dict[str, set[int]] = {}
    for vid, item in config.vlans.items():
        for name in item.untagged:
            untagged[name.lower()] = vid
        for name in item.tagged:
            tagged.setdefault(name.lower(), set()).add(vid)
    for name in sorted(set(untagged) | set(tagged)):
        target = port(name)
        if target is None:
            continue
        native = vlans[untagged[name]] if name in untagged else None
        mode = "trunk" if tagged.get(name) else "access"
        update = []
        if target.untagged_vlan_id != (native.pk if native else None):
            target.untagged_vlan = native
            update.append("untagged_vlan")
        if target.vlan_mode != mode:
            target.vlan_mode = mode
            update.append("vlan_mode")
        if update:
            target.save(update_fields=[*update, "updated_at"])
        have = set(TaggedVLAN.objects.filter(port=target).values_list("vlan__vid", flat=True))
        want = tagged.get(name, set())
        if have != want or update:
            TaggedVLAN.objects.filter(port=target).exclude(vlan__vid__in=want).delete()
            TaggedVLAN.objects.bulk_create(
                TaggedVLAN(tenant_id=target.tenant_id, port=target, vlan=vlans[vid])
                for vid in sorted(want - have)
            )
            result.add(
                _("Port %(port)s: untagged %(untagged)s, tagged %(tagged)s")
                % {
                    "port": target.name,
                    "untagged": native.vid if native else "-",
                    "tagged": ",".join(map(str, sorted(want))) or "-",
                }
            )


def _vlan_interface(
    request: HttpRequest,
    actor: User,
    device: Device,
    item: model.Vlan,
    ports: dict[str, Port],
    result: Result,
) -> None:
    """The routed interface of a VLAN (``vlan 100`` as a virtual port) with its addresses."""
    name = f"vlan {item.vid}"
    svi = ports.get(model.canonical(name))
    if svi is None:
        svi = Port.objects.create(
            device=device,
            name=name,
            kind=PortKind.VIRTUAL,
            connector=Connector.OTHER,
            order=20_000 + item.vid,
        )
        ports[model.canonical(name)] = svi
        result.add(_("New VLAN interface %(name)s") % {"name": name})
    wanted = {
        "description": "DHCP" if item.dhcp else svi.description,
        "ospf_area": item.ospf_area,
        "ospf_passive": item.ospf_passive,
    }
    changed = [k for k, v in wanted.items() if getattr(svi, k) != v]
    if changed:
        for key in changed:
            setattr(svi, key, wanted[key])
        svi.save(update_fields=[*changed, "updated_at"])
    _addresses(request, actor, device, svi, item.addresses, result)


def _addresses(
    request: HttpRequest,
    actor: User,
    device: Device,
    target: Port,
    addresses: list[str],
    result: Result,
) -> None:
    """Document the addresses of a port (taking over unassigned ones that already exist)."""
    for text in addresses:
        interface = ipaddress.ip_interface(text)
        existing = IPAddress.objects.filter(
            tenant_id=device.tenant_id, vrf__isnull=True, host=str(interface.ip)
        ).first()
        label = {"address": text, "name": target.name}
        if existing is None:
            ipam.create_address(request, actor, device.tenant, address=text, port=target)
            result.add(_("Address %(address)s on %(name)s") % label)
        elif existing.vm_interface_id is not None:
            result.skipped.append(
                _("Address %(address)s is on a virtual machine.") % {"address": text}
            )
        elif existing.port_id is None:
            ipam.update_address(request, actor, existing, port=target)
            result.add(_("Address %(address)s on %(name)s") % label)
        elif existing.port_id != target.pk:
            result.skipped.append(
                _("Address %(address)s is already on another port.") % {"address": text}
            )


def _replace(device: Device, config: model.Config, result: Result) -> None:
    """Lists the configuration owns completely: MST instances, routes, BGP peers, profiles."""
    tenant = device.tenant_id
    instances = [
        MSTInstance(tenant_id=tenant, device=device, instance=n, vlans=v, priority=p)
        for n, (v, p) in sorted(config.mst_instances.items())
    ]
    routes = [
        StaticRoute(
            tenant_id=tenant,
            device=device,
            prefix=r.prefix,
            next_hop=r.next_hop,
            distance=r.distance,
            interface=r.interface[:100],
        )
        for r in config.routes
    ]
    neighbors = [
        BGPNeighbor(
            tenant_id=tenant,
            device=device,
            address=n.address,
            remote_asn=n.remote_asn,
            description=n.description,
            enabled=n.enabled,
        )
        for n in config.neighbors.values()
    ]
    profiles = [
        PortProfile(
            tenant_id=tenant,
            device=device,
            name=p.name,
            detects=p.detects,
            untagged_vid=p.untagged,
            tagged_vids=p.tagged,
            jumbo=p.jumbo,
        )
        for p in config.profiles.values()
    ]
    lists: list[tuple[Any, list[Any], str, Any]] = [
        (MSTInstance, instances, _("MST instances"), lambda r: (r.instance, r.vlans, r.priority)),
        (
            StaticRoute,
            routes,
            _("static routes"),
            lambda r: (str(r.prefix), str(r.next_hop), r.distance, r.interface),
        ),
        (
            BGPNeighbor,
            neighbors,
            _("BGP neighbors"),
            lambda r: (str(r.address), r.remote_asn, r.description, r.enabled),
        ),
        (
            PortProfile,
            profiles,
            _("device profiles"),
            lambda r: (r.name, r.detects, r.untagged_vid, r.tagged_vids, r.jumbo),
        ),
    ]
    for table, rows, label, key in lists:
        valid, keys = [], []
        for row in rows:
            before = key(row)  # full_clean turns addresses into networks (/32)
            try:
                row.full_clean(exclude=["tenant", "device"])
            except ValidationError as exc:  # for example an MST instance above 64
                result.skipped.append(f"{label}: {'; '.join(exc.messages)}")
            else:
                valid.append(row)
                keys.append(before)
        current = list(table.objects.filter(device=device))
        if sorted(map(key, current), key=str) == sorted(keys, key=str):
            continue
        table.objects.filter(device=device).delete()
        table.objects.bulk_create(valid)
        result.add(_("%(what)s: %(n)s") % {"what": label, "n": len(valid)})


def _dhcp(
    request: HttpRequest, actor: User, device: Device, config: model.Config, result: Result
) -> None:
    """DHCP pools become scopes of documented prefixes (served by this device)."""
    from patchmanager.ipam.models import DHCPReservation, DHCPScope, Prefix

    relays = {
        str(ipaddress.ip_interface(a).network): v.helpers
        for v in config.vlans.values()
        for a in v.addresses
        if v.helpers
    }
    for pool in config.pools.values():
        if not (pool.network and pool.start and pool.end):
            continue
        prefix = Prefix.objects.filter(
            tenant_id=device.tenant_id, vrf__isnull=True, prefix=pool.network
        ).first()
        if prefix is None:
            result.skipped.append(
                _("DHCP pool %(pool)s: the network %(network)s is not documented in IP networks.")
                % {"pool": pool.name, "network": pool.network}
            )
            continue
        name = (pool.name or pool.network)[:100]
        scope = DHCPScope.objects.filter(tenant_id=device.tenant_id, name=name).first()
        if scope is not None and scope.server_id not in (None, device.pk):
            result.skipped.append(
                _("DHCP pool %(pool)s: a scope of that name belongs to another server.")
                % {"pool": pool.name}
            )
            continue
        allowed = (
            policy.has_perm(actor, "ipam.dhcpscope.change", scope)
            if scope is not None
            else policy.has_perm(actor, "ipam.dhcpscope.add", device.tenant)
        )
        if not allowed:
            result.skipped.append(
                _("DHCP pool %(pool)s: you may not document DHCP scopes.") % {"pool": pool.name}
            )
            continue
        created = scope is None
        fields = {
            "prefix": prefix,
            "server": device,
            "start": pool.start,
            "end": pool.end,
            "gateway": pool.gateway or None,
            "dns_servers": ", ".join(pool.dns)[:200],
            "domain_name": pool.domain,
            "lease_seconds": pool.lease_seconds or 86400,
            "relays": ", ".join(relays.get(pool.network, []))[:200],
        }
        change = None
        if scope is None:
            scope = DHCPScope(tenant_id=device.tenant_id, created_by=actor, name=name, **fields)
            change = _("New DHCP scope %(name)s") % {"name": name}
        elif any(str(getattr(scope, k) or "") != str(v or "") for k, v in fields.items()):
            for key, value in fields.items():
                setattr(scope, key, value)
            change = _("DHCP scope %(name)s changed") % {"name": name}
        try:
            scope.full_clean(exclude=["tenant"])
        except ValidationError as exc:
            result.skipped.append(f"{name}: {'; '.join(exc.messages)}")
            continue
        if change:  # only once it validates: a skipped scope is not also a change
            result.add(change)
        scope.save()
        audit.record(
            "ipam.dhcpscope.created" if created else "ipam.dhcpscope.changed",
            request=request,
            obj=scope,
            data={"name": name, "from": "configuration"},
        )
        have = {(r.mac_address, str(r.address).split("/")[0]) for r in scope.reservations.all()}
        want = {(mac, ip) for mac, ip, _name in pool.reservations}
        if have != want:
            scope.reservations.all().delete()
            for mac, ip, host in pool.reservations:
                reservation = DHCPReservation(
                    tenant_id=device.tenant_id,
                    scope=scope,
                    mac_address=mac,
                    address=ip,
                    hostname=host,
                )
                try:
                    reservation.full_clean(exclude=["tenant"])
                except ValidationError as exc:
                    result.skipped.append(f"{ip}: {'; '.join(exc.messages)}")
                    continue
                reservation.save()
            result.add(_("Reservations of %(name)s: %(n)s") % {"name": name, "n": len(want)})


def document(device: Device, user: Any) -> model.Config:
    """The documentation of a device as a configuration (to show or export)."""
    if not policy.has_perm(user, "dcim.device.view", device):
        raise ServiceError(_("Not found."))
    config = model.Config()
    settings = SwitchSettings.objects.filter(device=device).first()
    if settings is not None:
        config.hostname = settings.hostname
        config.stp_mode = settings.stp_mode
        config.stp_priority = settings.stp_priority
        config.mst_region = settings.mst_region
        config.mst_revision = settings.mst_revision
        config.ip_routing = settings.ip_routing
        config.bgp_asn = settings.bgp_asn
        config.bgp_router_id = str(settings.bgp_router_id or "")
        config.ospf_router_id = str(settings.ospf_router_id or "")
        config.gateways = list(settings.gateways)
        config.dns_servers = list(settings.dns_servers)
        config.domain = settings.domain
    jumbo = set(settings.jumbo_vlans) if settings else set()
    ports = list(
        Port.objects.filter(device=device)
        .select_related("untagged_vlan", "lag")
        .prefetch_related("tagged__vlan", "ip_addresses")
    )
    for p in ports:
        if p.kind == PortKind.LAG:
            members = [m.name for m in ports if m.lag_id == p.pk]
            config.trunks[p.name] = model.Trunk(p.name, members, p.lag_protocol or "static")
    for p in ports:
        if p.kind == PortKind.VIRTUAL and p.name.startswith("vlan "):
            vid = int(p.name.split()[1]) if p.name.split()[1].isdecimal() else None
            if vid is None:
                continue
            vlan = config.vlans.setdefault(vid, model.Vlan(vid))
            vlan.addresses = [str(a.address) for a in p.ip_addresses.all()]
            vlan.dhcp = p.description == "DHCP"
            vlan.ospf_area, vlan.ospf_passive = p.ospf_area, p.ospf_passive
            continue
        if p.kind not in (PortKind.INTERFACE, PortKind.LAG):
            continue
        if not p.lag_id:  # members of an aggregation carry its VLANs, not their own
            if p.untagged_vlan is not None:
                vid = p.untagged_vlan.vid
                config.vlans.setdefault(vid, model.Vlan(vid)).untagged.append(p.name)
            for tag in p.tagged.all():
                config.vlans.setdefault(tag.vlan.vid, model.Vlan(tag.vlan.vid)).tagged.append(
                    p.name
                )
        flags = (p.tunneled, p.stp_edge, p.stp_bpdu_guard, p.stp_root_guard)
        if p.kind == PortKind.INTERFACE and (p.label or not p.enabled or any(flags)):
            config.interfaces[p.name] = model.Interface(p.name, p.label, p.enabled, *flags)
    names = dict(
        VLAN.objects.filter(tenant_id=device.tenant_id, vid__in=list(config.vlans)).values_list(
            "vid", "name"
        )
    )
    for vid, vlan in config.vlans.items():
        vlan.name = names.get(vid, "")
        vlan.jumbo = vid in jumbo
    for row in MSTInstance.objects.filter(device=device):
        config.mst_instances[row.instance] = (list(row.vlans), row.priority)
    for route in StaticRoute.objects.filter(device=device):
        config.routes.append(
            model.Route(
                str(route.prefix),
                str(route.next_hop) if route.next_hop else None,
                route.distance,
                route.interface,
            )
        )
    for n in BGPNeighbor.objects.filter(device=device):
        config.neighbors[str(n.address)] = model.Neighbor(
            str(n.address), n.remote_asn, n.description, n.enabled
        )
    for profile in PortProfile.objects.filter(device=device):
        config.profiles[profile.name] = model.Profile(
            profile.name,
            profile.untagged_vid,
            list(profile.tagged_vids),
            profile.jumbo,
            profile.detects,
        )
    return config
