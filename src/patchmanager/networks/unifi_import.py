"""Import a UniFi OS backup: networks, WLANs, devices with their port VLANs, and cables.

Existing objects are matched and updated: VLANs by ID at the site, prefixes by network,
WLANs by SSID, devices by MAC address, serial number or name. Each part needs its import
right; a part the user may not import is left out with a note.
"""

import dataclasses
import ipaddress
import json
from typing import Any

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Q
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views.decorators.http import require_http_methods

from patchmanager.accounts import policy
from patchmanager.accounts.models import User
from patchmanager.accounts.services import PermissionDeniedError, ServiceError
from patchmanager.audit import services as audit
from patchmanager.core.fields import path_label
from patchmanager.dcim import devices as device_services
from patchmanager.dcim.models import Device, Port
from patchmanager.ipam import services as ipam
from patchmanager.ipam.models import Prefix
from patchmanager.library import services as library
from patchmanager.library.models import DeviceType, PortKind
from patchmanager.locations.models import Room
from patchmanager.networks import services, switchconfig, unifi
from patchmanager.networks.models import VLAN, WLAN
from patchmanager.networks.platforms import common as model
from patchmanager.networks.switchconfig import Result

VENDOR = "Ubiquiti"
CATEGORIES = {"usw": "switch", "uap": "access_point"}  # everything else routes: a firewall
SESSION_KEY = "unifi_import"
MAX_SESSION = 1024 * 1024  # what a preview keeps for applying, as JSON


class _Rollback(Exception):  # noqa: N818  # control flow, not an error
    """Leave the preview transaction without keeping anything."""


def _may(actor: User, tenant: Any, permission: str) -> bool:
    return policy.has_perm(actor, permission, scope_path=path_label(tenant.pk))


PARTS = {
    "networks": gettext_lazy("networks"),
    "wlans": gettext_lazy("WLANs"),
    "devices": gettext_lazy("devices"),
    "cables": gettext_lazy("cables"),
}
RIGHTS = {
    "networks": ("networks.vlan.import", "ipam.prefix.import"),
    "wlans": ("networks.wlan.import",),
    "devices": ("dcim.device.import",),
    "cables": ("cabling.cable.import",),
}


def allowed_parts(actor: User, tenant: Any) -> list[str]:
    """The parts of a backup this user may import into ``tenant``."""
    return [part for part, rights in RIGHTS.items() if all(_may(actor, tenant, r) for r in rights)]


def run(
    request: HttpRequest,
    actor: User,
    tenant: Any,
    backup: unifi.Backup,
    room: Room,
    *,
    commit: bool,
) -> Result:
    """Make the documentation match the backup (or show what that would do)."""
    if room.tenant_id != tenant.pk:
        raise PermissionDeniedError(_("You do not have permission to do that."))
    parts = allowed_parts(actor, tenant)
    if not parts:
        raise PermissionDeniedError(_("You do not have permission to do that."))
    result = Result()
    for part in RIGHTS:
        if part not in parts:
            result.skipped.append(_("You may not import %(part)s.") % {"part": PARTS[part]})
    try:
        with transaction.atomic():
            _Import(request, actor, tenant, backup, room, result).run(parts)
            if not commit:
                raise _Rollback
    except _Rollback:
        return result
    audit.record(
        "networks.unifi.imported",
        request=request,
        tenant_id=tenant.pk,
        data={"changes": len(result.changes), "devices": len(backup.devices)},
    )
    return result


class _Import:
    """One import run; keeps what earlier steps found for the later ones."""

    def __init__(
        self,
        request: HttpRequest,
        actor: User,
        tenant: Any,
        backup: unifi.Backup,
        room: Room,
        result: Result,
    ) -> None:
        self.request, self.actor, self.tenant = request, actor, tenant
        self.backup, self.room, self.result = backup, room, result
        self.site = room.floor.building.site
        self.lans = {n.id: n for n in backup.networks if n.vid is not None}
        self.devices: dict[str, Device] = {}  # by MAC address
        self.ports: dict[str, list[Port]] = {}  # interfaces in order, by MAC address

    def run(self, parts: list[str]) -> None:
        if "networks" in parts:
            for network in self.lans.values():
                self._guard(network.name, lambda n=network: self._network(n))
        if "wlans" in parts:
            for wlan in self.backup.wlans:
                self._guard(wlan.ssid, lambda w=wlan: self._wlan(w))
        if "devices" in parts:
            for item in self.backup.devices:
                self._guard(item.name or item.mac, lambda d=item: self._device(d))
            for item in self.backup.devices:
                if item.mac in self.devices:
                    self._guard(item.name or item.mac, lambda d=item: self._configure(d))
        if "cables" in parts:
            self._cables()

    def _guard(self, label: str, step: Any) -> None:
        """Run one step in a savepoint; a refused or invalid step is reported, not fatal."""
        try:
            with transaction.atomic():
                step()
        except (ServiceError, PermissionDeniedError) as exc:
            self.result.skipped.append(f"{label}: {exc}")

    # Networks: VLANs at the site and prefixes with their gateway.

    def _vlan(self, network: unifi.Network) -> VLAN:
        assert network.vid is not None  # noqa: S101  # nosec B101: LANs only
        vlan = (
            VLAN.objects.filter(tenant=self.tenant, vid=network.vid, site=self.site).first()
            or VLAN.objects.filter(tenant=self.tenant, vid=network.vid, site__isnull=True).first()
        )
        if vlan is None:
            name = network.name[:100] or f"VLAN{network.vid}"
            vlan = services.create_vlan(
                self.request, self.actor, self.tenant, site=self.site, vid=network.vid, name=name
            )
            self.result.add(
                _("New VLAN %(vid)s %(name)s at %(site)s")
                % {"vid": vlan.vid, "name": vlan.name, "site": self.site}
            )
        return vlan

    def _network(self, network: unifi.Network) -> None:
        vlan = self._vlan(network)
        cidr = unifi.prefix_of(network.subnet)
        if not cidr:
            return
        gateway = network.subnet.split("/")[0]
        prefix = Prefix.objects.filter(tenant=self.tenant, vrf__isnull=True, prefix=cidr).first()
        if prefix is None:
            ipam.create_prefix(
                self.request,
                self.actor,
                self.tenant,
                prefix=cidr,
                site=self.site,
                vlan=vlan,
                gateway=gateway,
                description=network.name,
            )
            self.result.add(_("New IP network %(prefix)s") % {"prefix": cidr})
            return
        wanted = {"vlan": vlan, "gateway": gateway}
        changed = {
            k: v for k, v in wanted.items() if str(getattr(prefix, k) or "").split("/")[0] != str(v)
        }
        if changed:
            ipam.update_prefix(self.request, self.actor, prefix, **changed)
            self.result.add(_("IP network %(prefix)s changed") % {"prefix": cidr})

    # WLANs, without their passphrase.

    def _wlan(self, item: unifi.Wlan) -> None:
        network = self.lans.get(item.network)
        vlan = self._vlan(network) if network is not None else None
        wanted = {"vlan": vlan, "security": item.security, "hidden": item.hidden}
        wlan = WLAN.objects.filter(tenant=self.tenant, ssid=item.ssid).first()
        if wlan is None:
            services.create_wlan(self.request, self.actor, self.tenant, ssid=item.ssid, **wanted)
            self.result.add(_("New WLAN %(ssid)s") % {"ssid": item.ssid})
            return
        changed = {k: v for k, v in wanted.items() if getattr(wlan, k) != v}
        if changed:
            services.update_wlan(self.request, self.actor, wlan, **changed)
            self.result.add(_("WLAN %(ssid)s changed") % {"ssid": item.ssid})

    # Devices: matched or created with a type, then their ports and addresses.

    def _match(self, item: unifi.Device) -> Device | None:
        rows = policy.scope_queryset(
            self.actor, "dcim.device.view", Device.objects.filter(tenant=self.tenant)
        )
        for lookup in (
            {"ports__mac_address__iexact": item.mac},  # the device MAC is on its first port
            {"serial__iexact": item.serial},
            {"name__iexact": item.name},
        ):
            if any(v for v in lookup.values()):
                found = rows.filter(**lookup).first()
                if found is not None:
                    return found
        return None

    def _type(self, item: unifi.Device) -> DeviceType:
        types = DeviceType.objects.filter(
            Q(tenant__isnull=True) | Q(tenant=self.tenant), vendor__name__iexact=VENDOR
        )
        found = (
            types.filter(model__iexact=item.model).first()
            or types.filter(part_number__iexact=item.model).first()
        )
        if found is not None:
            return found
        try:
            vendor = library.get_or_create_vendor(self.request, self.actor, VENDOR)
        except PermissionDeniedError as exc:  # vendors are shared: adding one needs a global grant
            raise ServiceError(
                _("%(vendor)s is not in the device library yet; an administrator can add it.")
                % {"vendor": VENDOR}
            ) from exc
        device_type = library.create_type(
            self.request,
            self.actor,
            tenant=self.tenant,
            vendor=vendor,
            model=item.model or item.kind,
            part_number=item.model,
            form=DeviceType.Form.FLOOR,
            category=CATEGORIES.get(item.kind, "firewall"),
        )
        library.add_port_template(
            self.request,
            self.actor,
            device_type,
            name_pattern="Port {n}",
            count=max(item.ports, 1),
            kind="interface",
            connector="rj45",
        )
        self.result.add(_("New device type %(type)s") % {"type": device_type})
        return device_type

    def _device(self, item: unifi.Device) -> None:
        device = self._match(item)
        wanted = {"name": item.name, "serial": item.serial}
        fields: dict[str, Any] = {k: v for k, v in wanted.items() if v}
        if device is None:
            device = device_services.create(
                self.request,
                self.actor,
                device_type=self._type(item),
                room=self.room,
                **fields,
            )
            self.result.add(_("New device %(name)s") % {"name": device.name})
        else:
            changed = {
                k: v for k, v in fields.items() if str(getattr(device, k)).lower() != v.lower()
            }
            if changed:
                device = device_services.update(self.request, self.actor, device, **changed)
                self.result.add(_("Device %(name)s changed") % {"name": device.name})
        ports = list(
            Port.objects.filter(device=device, kind=PortKind.INTERFACE).order_by("order", "name")
        )
        if ports and not any(p.mac_address.lower() == item.mac for p in ports):
            ports[0] = device_services.update_port(
                self.request, self.actor, ports[0], mac_address=item.mac
            )
        self.devices[item.mac] = device
        self.ports[item.mac] = ports

    def _configure(self, item: unifi.Device) -> None:
        device = self.devices[item.mac]
        config = self._config(item, self.ports[item.mac])
        found = switchconfig.apply(
            self.request,
            self.actor,
            device,
            config,
            commit=True,
            source="UniFi",
            merge=True,
        )
        self.result.changes += [f"{device.name}: {c}" for c in found.changes]
        self.result.skipped += [f"{device.name}: {s}" for s in found.skipped]

    def _config(self, item: unifi.Device, ports: list[Port]) -> model.Config:
        config = model.Config(hostname=item.name, vendor="unifi", platform=item.model)
        lans = [n for n in self.lans.values() if n.vid is not None]
        for network in lans:
            assert network.vid is not None  # noqa: S101  # nosec B101: LANs only
            config.vlans[network.vid] = model.Vlan(vid=network.vid, name=network.name[:100])
        if item.is_gateway:
            self._routes(config, lans)
        elif item.ip:
            self._management(config, item.ip, lans)
        if item.kind == "usw":
            self._ports(config, item, ports, lans)
        return config

    def _routes(self, config: model.Config, lans: list[unifi.Network]) -> None:
        """A gateway carries the address of every network and serves their DHCP."""
        for network in lans:
            cidr = unifi.prefix_of(network.subnet)
            if not cidr or network.vid is None:
                continue
            vlan = config.vlans[network.vid]
            vlan.addresses.append(network.subnet)
            vlan.dhcp = network.dhcp
            if not (network.dhcp and network.dhcp_start and network.dhcp_stop):
                continue
            inside = ipaddress.ip_network(cidr)
            config.pools[network.name or cidr] = model.Pool(
                name=(network.name or cidr)[:100],
                network=cidr,
                start=network.dhcp_start,
                end=network.dhcp_stop,
                gateway=network.subnet.split("/")[0],
                dns=network.dns,
                domain=network.domain,
                lease_seconds=network.lease_seconds,
                reservations=[
                    (c.mac, c.ip, c.name) for c in self.backup.clients if _inside(c.ip, inside)
                ],
            )

    @staticmethod
    def _management(config: model.Config, ip: str, lans: list[unifi.Network]) -> None:
        """The management address sits on the VLAN interface of the network it is in."""
        for network in lans:
            cidr = unifi.prefix_of(network.subnet)
            if cidr and network.vid is not None and _inside(ip, ipaddress.ip_network(cidr)):
                length = ipaddress.ip_network(cidr).prefixlen
                config.vlans[network.vid].addresses.append(f"{ip}/{length}")
                return

    def _ports(
        self, config: model.Config, item: unifi.Device, ports: list[Port], lans: list[unifi.Network]
    ) -> None:
        """Port VLANs: UniFi carries every network on a port unless a port profile limits it."""
        default = next((n for n in lans if n.vid == 1), None)
        for idx, port in enumerate(ports[: item.ports], start=1):
            override = item.overrides.get(idx, unifi.PortOverride())
            native = self.lans.get(override.native) or default
            if override.tagged == "block_all":
                tagged: list[unifi.Network] = []
            else:
                excluded = set(override.excluded) if override.tagged == "custom" else set()
                tagged = [n for n in lans if n is not native and n.id not in excluded]
            if native is not None and native.vid is not None:
                config.vlans[native.vid].untagged.append(port.name)
            for network in tagged:
                if network.vid is not None:
                    config.vlans[network.vid].tagged.append(port.name)
            if idx in item.overrides:
                config.interfaces[port.name] = model.Interface(
                    name=port.name, label=override.name, enabled=override.enabled
                )

    # Cables between UniFi devices, from each device's uplink.

    def _cables(self) -> None:
        done: set[frozenset[Any]] = set()
        for item in self.backup.devices:
            if not (item.uplink_mac and item.uplink_port and item.uplink_remote_port):
                continue
            a = _nth(self.ports.get(item.mac, []), item.uplink_port)
            b = _nth(self.ports.get(item.uplink_mac, []), item.uplink_remote_port)
            if a is None or b is None or frozenset((a.pk, b.pk)) in done:
                continue
            done.add(frozenset((a.pk, b.pk)))
            label = f"{a.device.name} {a.name} / {b.device.name} {b.name}"
            peer = _peer(a)
            if peer is not None:
                if peer.pk != b.pk:
                    self.result.skipped.append(
                        _("%(cable)s: a port already has another cable.") % {"cable": label}
                    )
                continue
            if _peer(b) is not None:
                self.result.skipped.append(
                    _("%(cable)s: a port already has another cable.") % {"cable": label}
                )
                continue
            self._guard(label, lambda a=a, b=b, label=label: self._connect(a, b, label))

    def _connect(self, a: Port, b: Port, label: str) -> None:
        from patchmanager.cabling import services as cabling

        cabling.connect(self.request, self.actor, a, b)
        self.result.add(_("New cable %(cable)s") % {"cable": label})


def _nth(ports: list[Port], idx: int) -> Port | None:
    return ports[idx - 1] if 0 < idx <= len(ports) else None


def _peer(port: Port) -> Port | None:
    """The port at the other end of this port's cable."""
    from patchmanager.cabling.models import CableEnd

    end = CableEnd.objects.filter(port=port).first()
    if end is None:
        return None
    other = CableEnd.objects.filter(cable_id=end.cable_id).exclude(port=port).first()
    return other.port if other is not None else port  # an open end counts as cabled


def _inside(ip: str, network: Any) -> bool:
    try:
        return ipaddress.ip_address(ip) in network
    except ValueError:
        return False


# Page: upload, preview, apply.


class ImportForm(forms.Form):
    """A backup and the room new devices go to."""

    file = forms.FileField(label=gettext_lazy("UniFi OS backup (.unifi)"), required=False)
    room = forms.ModelChoiceField(
        label=gettext_lazy("Room for new devices"), queryset=Room.objects.none()
    )

    def __init__(self, *args: Any, rooms: Any, **kwargs: Any) -> None:
        """Offer the rooms of the tenant the user may add devices to."""
        super().__init__(*args, **kwargs)
        field: Any = self.fields["room"]
        field.queryset = rooms

    def clean_file(self) -> Any:
        """At most 64 MB."""
        upload = self.cleaned_data.get("file")
        if upload is not None and upload.size > unifi.MAX_FILE:
            raise forms.ValidationError(_("The backup is larger than 64 MB."))
        return upload


def _to_session(request: HttpRequest, tenant: Any, backup: unifi.Backup) -> None:
    data = json.dumps({"tenant": str(tenant.pk), **dataclasses.asdict(backup)})
    if len(data) > MAX_SESSION:
        raise ServiceError(_("The backup is too large to read."))
    request.session[SESSION_KEY] = data  # names and addresses only; no secrets were read


def _from_session(request: HttpRequest, tenant: Any) -> unifi.Backup | None:
    raw = request.session.get(SESSION_KEY)
    if not raw:
        return None
    data = json.loads(raw)
    if data.get("tenant") != str(tenant.pk):  # read while another tenant was selected
        return None
    return unifi.Backup(
        sites=data["sites"],
        networks=[unifi.Network(**n) for n in data["networks"]],
        wlans=[unifi.Wlan(**w) for w in data["wlans"]],
        devices=[
            unifi.Device(
                **{
                    **d,
                    "overrides": {
                        int(k): unifi.PortOverride(**v) for k, v in d["overrides"].items()
                    },
                }
            )
            for d in data["devices"]
        ],
        clients=[unifi.Client(**c) for c in data["clients"]],
    )


@login_required
@require_http_methods(["GET", "POST"])
def import_view(request: HttpRequest) -> HttpResponse:
    """Read a UniFi OS backup, show what changes, then apply it."""
    tenant = getattr(request, "tenant", None)
    if tenant is None:
        raise Http404
    actor: Any = request.user
    parts = allowed_parts(actor, tenant)
    if not parts:
        raise PermissionDenied
    rooms = policy.scope_queryset(
        actor, "dcim.device.add", Room.objects.filter(tenant=tenant)
    ).select_related("floor__building__site")
    form = ImportForm(request.POST or None, request.FILES or None, rooms=rooms)
    result = None
    if request.method == "POST" and form.is_valid():
        room = form.cleaned_data["room"]
        try:
            if request.POST.get("action") == "apply":
                backup = _from_session(request, tenant)
                if backup is None:
                    raise ServiceError(_("Read the backup again."))
                result = run(request, actor, tenant, backup, room, commit=True)
                request.session.pop(SESSION_KEY, None)
                messages.success(
                    request, _("UniFi backup imported: %(n)s changes.") % {"n": len(result.changes)}
                )
                for note in result.skipped[:10]:
                    messages.warning(request, note)
                return redirect("devices:devices")
            upload = form.cleaned_data.get("file")
            if upload is None:
                raise ServiceError(_("Choose a UniFi OS backup."))
            try:
                backup = unifi.read(upload.read())
            except unifi.ReadError as exc:
                raise ServiceError(str(exc)) from exc
            backup = backup.only(backup.busiest_site())
            _to_session(request, tenant, backup)
            result = run(request, actor, tenant, backup, room, commit=False)
        except PermissionDeniedError as exc:
            raise PermissionDenied from exc
        except ServiceError as exc:
            form.add_error(None, str(exc))
    return render(
        request,
        "networks/unifi_import.html",
        {"form": form, "result": result, "parts": [PARTS[p] for p in parts]},
    )
