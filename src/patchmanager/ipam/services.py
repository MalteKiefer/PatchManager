"""VRFs, prefixes and IP addresses: changes with permission checks, and address planning."""

import ipaddress
from dataclasses import dataclass
from typing import Any

from django.core.exceptions import ValidationError
from django.db import DatabaseError, IntegrityError, transaction
from django.db.models import ProtectedError, QuerySet
from django.http import HttpRequest
from django.utils.translation import gettext as _

from patchmanager.accounts.models import User
from patchmanager.accounts.services import ServiceError
from patchmanager.accounts.services.users import require
from patchmanager.audit import services as audit
from patchmanager.core.fields import path_label
from patchmanager.ipam.models import VRF, IPAddress, Prefix
from patchmanager.tenancy.models import Tenant

MAX_LISTED_FREE = 64  # free ranges shown for one prefix


def _save(obj: Any, duplicate: str) -> None:
    try:
        obj.full_clean(exclude=["tenant", "path"])
    except ValidationError as exc:
        if any("already exists" in m.lower() for m in exc.messages):
            raise ServiceError(duplicate) from exc
        raise ServiceError("; ".join(exc.messages)) from exc
    try:
        with transaction.atomic():
            obj.save()
    except IntegrityError as exc:
        raise ServiceError(duplicate) from exc
    except DatabaseError as exc:
        raise ServiceError(_("This does not fit the tenant, or the gateway is outside.")) from exc


def _prefix_path(prefix: Prefix) -> str:
    return str(prefix.site.path) if prefix.site is not None else path_label(prefix.tenant_id)


def _address_path(address: IPAddress) -> str:
    if address.port is not None:
        return str(address.port.device.path)
    return path_label(address.tenant_id)


# --- VRFs -----------------------------------------------------------------


def create_vrf(request: HttpRequest, actor: User, tenant: Tenant, **fields: Any) -> VRF:
    """Create a VRF in a tenant."""
    require(actor, "ipam.vrf.add", scope_path=path_label(tenant.pk))
    vrf = VRF(tenant=tenant, created_by=actor, **fields)
    _save(vrf, _("A VRF with this name already exists."))
    audit.record("ipam.vrf.created", request=request, obj=vrf)
    return vrf


def update_vrf(request: HttpRequest, actor: User, vrf: VRF, **fields: Any) -> VRF:
    """Change a VRF."""
    require(actor, "ipam.vrf.change", vrf)
    for key, value in fields.items():
        setattr(vrf, key, value)
    _save(vrf, _("A VRF with this name already exists."))
    audit.record("ipam.vrf.changed", request=request, obj=vrf)
    return vrf


def delete_vrf(request: HttpRequest, actor: User, vrf: VRF) -> None:
    """Delete an empty VRF."""
    require(actor, "ipam.vrf.delete", vrf)
    try:
        with transaction.atomic():
            vrf.delete()
    except ProtectedError as exc:
        raise ServiceError(_("Prefixes or addresses still use this VRF.")) from exc
    audit.record("ipam.vrf.deleted", request=request, data={"name": vrf.name})


# --- Prefixes -------------------------------------------------------------


def create_prefix(request: HttpRequest, actor: User, tenant: Tenant, **fields: Any) -> Prefix:
    """Create a prefix in a tenant, optionally at a site."""
    prefix = Prefix(tenant=tenant, created_by=actor, **fields)
    prefix.path = _prefix_path(prefix)
    require(actor, "ipam.prefix.add", scope_path=prefix.path)
    _save(prefix, _("This prefix already exists in this VRF."))
    audit.record("ipam.prefix.created", request=request, obj=prefix)
    return prefix


def update_prefix(request: HttpRequest, actor: User, prefix: Prefix, **fields: Any) -> Prefix:
    """Change a prefix (authorised on the stored row and on the result)."""
    stored = Prefix.objects.get(pk=prefix.pk)
    require(actor, "ipam.prefix.change", stored)
    for key, value in fields.items():
        setattr(stored, key, value)
    stored.path = _prefix_path(stored)
    require(actor, "ipam.prefix.change", stored)
    _save(stored, _("This prefix already exists in this VRF."))
    audit.record("ipam.prefix.changed", request=request, obj=stored)
    return stored


def delete_prefix(request: HttpRequest, actor: User, prefix: Prefix) -> None:
    """Delete a prefix; the addresses inside stay."""
    require(actor, "ipam.prefix.delete", prefix)
    try:
        prefix.delete()
    except ProtectedError as exc:  # DHCP scopes in the prefix
        raise ServiceError(_("Other records still use this prefix.")) from exc
    audit.record("ipam.prefix.deleted", request=request, data={"prefix": prefix.prefix})


# --- Addresses ------------------------------------------------------------


def create_address(
    request: HttpRequest | None, actor: User, tenant: Tenant, **fields: Any
) -> IPAddress:
    """Create an address, optionally on a port of the tenant."""
    address = IPAddress(tenant=tenant, created_by=actor, **fields)
    address.path = _address_path(address)
    require(actor, "ipam.ipaddress.add", scope_path=address.path)
    if address.port is not None:
        require(actor, "dcim.device.change", address.port.device)
    if address.vm_interface is not None:
        require(actor, "compute.vm.change", address.vm_interface.vm)
    _save(address, _("This address is already used in this VRF."))
    audit.record("ipam.ipaddress.created", actor=actor, request=request, obj=address)
    return address


def update_address(
    request: HttpRequest | None, actor: User, address: IPAddress, **fields: Any
) -> IPAddress:
    """Change an address or move it to another port."""
    stored = IPAddress.objects.select_related("port__device").get(pk=address.pk)
    if "vm_interface" in fields and fields["vm_interface"] is not None:
        require(actor, "compute.vm.change", fields["vm_interface"].vm)
    require(actor, "ipam.ipaddress.change", stored)
    for key, value in fields.items():
        setattr(stored, key, value)
    stored.path = _address_path(stored)
    require(actor, "ipam.ipaddress.change", stored)
    if "port" in fields and stored.port is not None:
        require(actor, "dcim.device.change", stored.port.device)
    _save(stored, _("This address is already used in this VRF."))
    audit.record("ipam.ipaddress.changed", actor=actor, request=request, obj=stored)
    return stored


def delete_address(request: HttpRequest, actor: User, address: IPAddress) -> None:
    """Delete an address."""
    require(actor, "ipam.ipaddress.delete", address)
    address.delete()
    audit.record("ipam.ipaddress.deleted", request=request, data={"address": address.address})


def refresh_paths(device: Any) -> int:
    """Re-scope the addresses on ``device`` after it moved."""
    return IPAddress.objects.filter(port__device=device).update(path=str(device.path))


# --- Planning -------------------------------------------------------------


def children(prefix: Prefix, prefixes: QuerySet[Prefix]) -> list[Prefix]:
    """Direct children: prefixes inside ``prefix`` with no other prefix in between."""
    inside = list(
        prefixes.filter(vrf=prefix.vrf, tenant_id=prefix.tenant_id).filter(
            prefix__net_within=prefix.prefix
        )
    )
    nets: list[tuple[Prefix, Any]] = [(p, p.network) for p in inside]
    return [
        p
        for p, net in nets
        if not any(other is not p and net != o and net.subnet_of(o) for other, o in nets)
    ]


def addresses(prefix: Prefix, visible: QuerySet[IPAddress]) -> QuerySet[IPAddress]:
    """Addresses of the same VRF inside the prefix."""
    return visible.filter(
        vrf=prefix.vrf, tenant_id=prefix.tenant_id, address__net_within_or_equal=prefix.prefix
    ).order_by("address")


def _hosts(network: Any) -> tuple[int, int]:
    """First and last usable address as integers (network and broadcast excluded on IPv4)."""
    first, last = int(network.network_address), int(network.broadcast_address)
    if network.version == 4 and network.prefixlen < 31:
        first, last = first + 1, last - 1
    return first, last


@dataclass(frozen=True)
class Usage:
    """How much of a prefix is taken by child prefixes and addresses."""

    used: int
    size: int

    @property
    def percent(self) -> int:
        """Rounded share (at least 1 when anything is used)."""
        if not self.size:
            return 0
        share = round(100 * self.used / self.size)
        return max(share, 1) if self.used else 0


def usage(prefix: Prefix, child_prefixes: list[Prefix], taken: list[str]) -> Usage:
    """Containers count child prefixes, others count addresses."""
    network = prefix.network
    if prefix.status == Prefix.Status.CONTAINER:
        used = sum(p.network.num_addresses for p in child_prefixes)
        return Usage(used, network.num_addresses)
    first, last = _hosts(network)
    return Usage(len(set(taken)), max(last - first + 1, 0))


def free_ranges(prefix: Prefix, taken: list[str]) -> list[tuple[str, str, int]]:
    """Unused host ranges ``(first, last, count)`` in address order."""
    network = prefix.network
    first, last = _hosts(network)
    used = sorted(
        {
            int(ipaddress.ip_address(h))
            for h in taken
            if first <= int(ipaddress.ip_address(h)) <= last
        }
    )
    ranges = []
    start = first
    for value in [*used, last + 1]:
        if value > start:
            ranges.append((start, value - 1))
            if len(ranges) >= MAX_LISTED_FREE:
                break
        start = value + 1
    make = ipaddress.ip_address
    return [(str(make(a)), str(make(b)), b - a + 1) for a, b in ranges]


def next_free(prefix: Prefix, taken: list[str]) -> str | None:
    """The first unused host address, with the prefix length (``10.0.0.7/24``)."""
    ranges = free_ranges(prefix, taken)
    if not ranges:
        return None
    return f"{ranges[0][0]}/{prefix.network.prefixlen}"
