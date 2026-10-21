"""VLANs, WLANs and their use on ports."""

from typing import Any

from django.core.exceptions import ValidationError
from django.db import DatabaseError, IntegrityError, transaction
from django.db.models import ProtectedError
from django.http import HttpRequest
from django.utils.translation import gettext as _

from patchmanager.accounts import policy
from patchmanager.accounts.models import User
from patchmanager.accounts.services import PermissionDeniedError, ServiceError
from patchmanager.accounts.services.users import require
from patchmanager.audit import services as audit
from patchmanager.core.fields import path_label
from patchmanager.dcim.models import Port
from patchmanager.networks.models import VLAN, WLAN, TaggedVLAN, WLANBroadcast
from patchmanager.tenancy.models import Tenant


def _save(obj: Any, duplicate: str) -> None:
    try:
        obj.full_clean(exclude=["tenant"])
    except ValidationError as exc:
        if any("unique" in m.lower() or "already exists" in m.lower() for m in exc.messages):
            raise ServiceError(duplicate) from exc
        raise ServiceError("; ".join(exc.messages)) from exc
    try:
        with transaction.atomic():
            obj.save()
    except IntegrityError as exc:
        raise ServiceError(duplicate) from exc
    except DatabaseError as exc:
        raise ServiceError(_("This does not fit the tenant or site.")) from exc


def _scope(tenant: Tenant, site: Any) -> str:
    return str(site.path) if site is not None else path_label(tenant.pk)


def create_vlan(request: HttpRequest, actor: User, tenant: Tenant, **fields: Any) -> VLAN:
    """Create a VLAN in a tenant, optionally limited to one site."""
    require(actor, "networks.vlan.add", scope_path=_scope(tenant, fields.get("site")))
    vlan = VLAN(tenant=tenant, created_by=actor, **fields)
    _save(vlan, _("This VLAN ID is already used here."))
    audit.record("networks.vlan.created", request=request, obj=vlan, data={"vid": vlan.vid})
    return vlan


def update_vlan(request: HttpRequest, actor: User, vlan: VLAN, **fields: Any) -> VLAN:
    """Change a VLAN."""
    stored = VLAN.objects.get(pk=vlan.pk)
    require(actor, "networks.vlan.change", stored)
    for key, value in fields.items():
        setattr(stored, key, value)
    require(actor, "networks.vlan.change", stored)
    if "site" in fields and (stored.untagged_on.exists() or stored.tagged_on.exists()):
        original = VLAN.objects.get(pk=vlan.pk)
        if original.site_id != stored.site_id:
            raise ServiceError(_("The site of a VLAN in use cannot change."))
    _save(stored, _("This VLAN ID is already used here."))
    audit.record("networks.vlan.changed", request=request, obj=stored)
    return stored


def delete_vlan(request: HttpRequest, actor: User, vlan: VLAN) -> None:
    """Delete a VLAN; ports using it lose the assignment."""
    require(actor, "networks.vlan.delete", vlan)
    audit.record("networks.vlan.deleted", request=request, obj=vlan, data={"vid": vlan.vid})
    try:
        with transaction.atomic():
            vlan.delete()
    except ProtectedError as exc:
        raise ServiceError(_("A WLAN still uses this VLAN.")) from exc


def create_wlan(request: HttpRequest, actor: User, tenant: Tenant, **fields: Any) -> WLAN:
    """Create a WLAN."""
    require(actor, "networks.wlan.add", scope_path=path_label(tenant.pk))
    wlan = WLAN(tenant=tenant, created_by=actor, **fields)
    _save(wlan, _("This SSID already exists."))
    audit.record("networks.wlan.created", request=request, obj=wlan)
    return wlan


def update_wlan(request: HttpRequest, actor: User, wlan: WLAN, **fields: Any) -> WLAN:
    """Change a WLAN."""
    require(actor, "networks.wlan.change", wlan)
    stored = WLAN.objects.get(pk=wlan.pk)
    for key, value in fields.items():
        setattr(stored, key, value)
    _save(stored, _("This SSID already exists."))
    audit.record("networks.wlan.changed", request=request, obj=stored)
    return stored


def delete_wlan(request: HttpRequest, actor: User, wlan: WLAN) -> None:
    """Delete a WLAN."""
    require(actor, "networks.wlan.delete", wlan)
    audit.record("networks.wlan.deleted", request=request, obj=wlan)
    wlan.delete()


def set_broadcast(request: HttpRequest, actor: User, wlan: WLAN, port: Port, *, on: bool) -> None:
    """Let a wireless port broadcast the WLAN (or stop it)."""
    require(actor, "networks.wlan.change", wlan)
    if not policy.has_perm(actor, "dcim.device.change", port.device):
        raise PermissionDeniedError(_("You do not have permission to do that."))
    if on:
        if port.kind != "wireless":
            raise ServiceError(_("Only wireless ports broadcast WLANs."))
        try:
            with transaction.atomic():
                WLANBroadcast.objects.get_or_create(tenant_id=wlan.tenant_id, wlan=wlan, port=port)
        except DatabaseError as exc:
            raise ServiceError(_("This does not fit the tenant or site.")) from exc
    else:
        WLANBroadcast.objects.filter(wlan=wlan, port=port).delete()
    audit.record(
        "networks.wlan.broadcast_changed",
        request=request,
        obj=wlan,
        data={"port": f"{port.device}:{port.name}", "on": on},
    )


def set_port_vlans(
    request: HttpRequest,
    actor: User,
    port: Port,
    *,
    mode: str,
    untagged: VLAN | None,
    tagged: list[VLAN],
) -> None:
    """Access port: one untagged VLAN. Trunk: optional native VLAN plus tagged VLANs."""
    if not policy.has_perm(actor, "dcim.device.change", port.device):
        raise PermissionDeniedError(_("You do not have permission to do that."))
    if mode not in ("", "access", "trunk"):
        raise ServiceError(_("Unknown VLAN mode."))
    if mode == "access" and tagged:
        raise ServiceError(_("Access ports carry no tagged VLANs."))
    if mode == "" and (untagged or tagged):
        raise ServiceError(_("Choose access or trunk to assign VLANs."))
    for vlan in [untagged, *tagged]:
        if vlan is not None and not policy.has_perm(actor, "networks.vlan.view", vlan):
            raise PermissionDeniedError(_("You do not have permission to do that."))
    try:
        with transaction.atomic():
            Port.objects.filter(pk=port.pk).update(vlan_mode=mode, untagged_vlan=untagged)
            TaggedVLAN.objects.filter(port=port).delete()
            TaggedVLAN.objects.bulk_create(
                TaggedVLAN(tenant_id=port.tenant_id, port=port, vlan=v) for v in tagged
            )
    except DatabaseError as exc:
        raise ServiceError(
            _("The VLANs must belong to this tenant and to the device's site.")
        ) from exc
    audit.record(
        "dcim.port.vlans_changed",
        request=request,
        obj=port.device,
        data={
            "port": port.name,
            "mode": mode,
            "untagged": getattr(untagged, "vid", None),
            "tagged": sorted(v.vid for v in tagged),
        },
    )


def vlans_for_port(port: Port, user: Any = None) -> Any:
    """VLANs usable on ``port`` (tenant wide ones and those of its site) that ``user`` may see."""
    from django.db.models import Q

    site_label = str(port.device.path).split(".")[1]
    candidates = VLAN.objects.filter(tenant_id=port.tenant_id).filter(
        Q(site__isnull=True) | Q(site__path=f"{str(port.device.path).split('.')[0]}.{site_label}")
    )
    if user is None:
        return candidates
    allowed = [v.pk for v in candidates if policy.has_perm(user, "networks.vlan.view", v)]
    return VLAN.objects.filter(pk__in=allowed)
