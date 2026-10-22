"""Create, change, delete and move nodes of the location tree (sites to racks)."""

from typing import Any

from django.core.exceptions import ValidationError
from django.db import DatabaseError, IntegrityError, transaction
from django.db.models import ProtectedError
from django.http import HttpRequest
from django.utils.translation import gettext as _

from patchmanager.accounts import policy
from patchmanager.accounts.models import Assignment, User
from patchmanager.accounts.services import ServiceError
from patchmanager.accounts.services.users import require
from patchmanager.audit import services as audit
from patchmanager.core.fields import path_label
from patchmanager.core.hierarchy import HierarchyNode
from patchmanager.core.utils import key_fields
from patchmanager.tenancy.models import Tenant
from patchmanager.tenancy.rls import system_context


def _save(node: HierarchyNode) -> None:
    try:
        node.full_clean(exclude=["path", "tenant"])
    except ValidationError as exc:
        if any("unique" in str(m) for m in exc.messages):
            raise ServiceError(_("An object with this name already exists here.")) from exc
        raise ServiceError(validate_message(exc)) from exc
    try:
        with transaction.atomic():
            node.save()
    except IntegrityError as exc:
        raise ServiceError(_("An object with this name already exists here.")) from exc
    except DatabaseError as exc:  # integrity triggers, for example shrinking a used rack
        raise ServiceError(_("This change conflicts with existing data.")) from exc


def create[N: HierarchyNode](
    request: HttpRequest, actor: User, model: type[N], *, parent: Any, **fields: Any
) -> N:
    """Create a node below ``parent`` (a tenant for sites)."""
    scope = parent.policy_path
    require(actor, f"{model.policy_object_type}.add", scope_path=scope)
    node = model(created_by=actor, **key_fields(fields, "kind"))
    if model.parent_field:
        setattr(node, model.parent_field, parent)
    else:
        node.tenant = parent
    _save(node)
    audit.record(
        f"{model.policy_object_type}.created", request=request, obj=node, data={"name": str(node)}
    )
    return node


def update[N: HierarchyNode](request: HttpRequest, actor: User, node: N, **fields: Any) -> N:
    """Change attributes of a node (not its parent).

    Authorisation uses the stored row, never ``node`` itself: bound model forms
    copy submitted values onto their instance during validation, which would
    let attribute conditions judge the attacker's input. The resulting state
    must be allowed too, so nobody moves an object out of their own grant.
    """
    perm = f"{node.policy_object_type}.change"
    stored = type(node)._default_manager.get(pk=node.pk)
    require(actor, perm, stored)
    fields = key_fields(fields, "kind")
    before = {key: str(getattr(stored, key)) for key in fields}
    for key, value in fields.items():
        setattr(stored, key, value)
    require(actor, perm, stored)
    _save(stored)
    node.refresh_from_db()
    changed = {k: [before[k], str(v)] for k, v in fields.items() if before[k] != str(v)}
    audit.record(
        f"{node.policy_object_type}.changed", request=request, obj=node, data={"changed": changed}
    )
    return node


def delete(request: HttpRequest, actor: User, node: HierarchyNode) -> None:
    """Delete an empty node."""
    require(actor, f"{node.policy_object_type}.delete", node)
    audit.record(
        f"{node.policy_object_type}.deleted", request=request, obj=node, data={"name": str(node)}
    )
    from patchmanager.locations.models import Room
    from patchmanager.metadata import services as metadata

    try:
        with transaction.atomic():
            # Photos and notes go with the object (rolled back if it cannot be deleted).
            metadata.purge(node)
            if isinstance(node, Room):
                for survey in node.surveys.all():
                    metadata.purge(survey)
            node.delete()
    except ProtectedError as exc:
        raise ServiceError(_("Remove everything inside it first.")) from exc


# Replace the ``old`` prefix of a path with ``new``. Parameters: old, new, new, old.
_REBASE = (
    "CASE WHEN {column} = %s::ltree THEN %s::ltree"
    " ELSE %s::ltree || subpath({column}, nlevel(%s::ltree)) END"
)
_TREE_TABLES = (
    "locations_site",
    "locations_building",
    "locations_floor",
    "locations_room",
    "dcim_rack",
    "dcim_device",
)


def _moving_ids(site: Any, devices: Any) -> list[Any]:
    """Every object that moves with a site and could be linked to a system or customer."""
    from django.apps import apps

    from patchmanager.configs.models import DeviceConfig
    from patchmanager.networks.models import VLAN, Stack

    ids: list[Any] = []
    for table in _TREE_TABLES:
        app_label, model_name = table.split("_", 1)
        model = apps.get_model(app_label, model_name)
        ids += model.objects.filter(path__descendant_of=site.path).values_list("pk", flat=True)
    ids += DeviceConfig.objects.filter(device__in=devices).values_list("pk", flat=True)
    ids += VLAN.objects.filter(site=site).values_list("pk", flat=True)
    ids += Stack.objects.filter(site=site).values_list("pk", flat=True)
    return ids


def _check_site_is_self_contained(site: Any, device_model: Any) -> None:
    """Cables and tenant wide networks would otherwise end up spanning two tenants."""
    from patchmanager.cabling.models import CableEnd
    from patchmanager.networks.models import VLAN, WLAN, TaggedVLAN, WLANBroadcast

    inside_ports = device_model.objects.filter(path__descendant_of=site.path).values("ports")
    inside_cables = CableEnd.objects.filter(port__in=inside_ports).values("cable_id")
    if CableEnd.objects.filter(cable_id__in=inside_cables).exclude(port__in=inside_ports).exists():
        raise ServiceError(
            _("Cables connect this site to other sites of the tenant. Remove them first.")
        )
    from patchmanager.ipam.models import IPAddress, Prefix

    if (
        Prefix.objects.filter(site=site).exists()
        or IPAddress.objects.filter(port__in=inside_ports).exists()
    ):
        raise ServiceError(
            _("Prefixes or IP addresses belong to this site. Move or remove them first.")
        )
    if (
        WLANBroadcast.objects.filter(port__in=inside_ports).exists()
        or VLAN.objects.filter(site__isnull=True, untagged_on__in=inside_ports).exists()
        or TaggedVLAN.objects.filter(port__in=inside_ports, vlan__site__isnull=True).exists()
        or WLAN.objects.filter(vlan__site=site).exists()
    ):
        raise ServiceError(
            _("Ports in this site use tenant wide VLANs or WLANs. Remove those assignments first.")
        )
    from patchmanager.circuits.models import Circuit
    from patchmanager.directory.models import Link, System
    from patchmanager.ipam.models import DHCPScope, DNSZone

    inside = device_model.objects.filter(path__descendant_of=site.path)
    if (
        System.objects.filter(site=site).exists()
        or Circuit.objects.filter(site=site).exists()
        or Circuit.objects.filter(port__in=inside_ports).exists()
        or DHCPScope.objects.filter(server__in=inside).exists()
        or DNSZone.objects.filter(server__in=inside).exists()
        or inside.filter(supplier__isnull=False).exists()
        or Link.objects.filter(object_id__in=_moving_ids(site, inside)).exists()
    ):
        raise ServiceError(
            _(
                "Systems, lines, suppliers, customers or DHCP and DNS servers refer to this site."
                " Remove those references first."
            )
        )


def move_site(request: HttpRequest, actor: User, site: Any, target: Tenant) -> None:
    """Move a site with everything below it to another tenant.

    Paths and tenant ids of every node, rack space and assignment below the
    site are rewritten in one transaction, top down so the hierarchy trigger
    accepts each row.
    """
    require(actor, "locations.site.move_tenant", site)
    require(actor, "locations.site.add", scope_path=target.policy_path)
    if target.pk == site.tenant_id:
        raise ServiceError(_("The site already belongs to this tenant."))
    from patchmanager.dcim.models import Device

    if Device.objects.filter(
        path__descendant_of=site.path, device_type__tenant__isnull=False
    ).exists():
        raise ServiceError(
            _("Devices in this site use device types private to the current tenant.")
        )
    _check_site_is_self_contained(site, Device)
    source_tenant = site.tenant_id
    old = str(site.path)
    new = f"{path_label(target.pk)}.{path_label(site.pk)}"
    with transaction.atomic(), system_context():
        from django.db import connection

        with connection.cursor() as cursor:
            for table in _TREE_TABLES:
                cursor.execute(
                    f"UPDATE {table} SET tenant_id = %s, "  # noqa: S608  # nosec B608: fixed table names
                    f"path = {_REBASE.format(column='path')} WHERE path <@ %s::ltree",
                    [target.pk, old, new, new, old, old],
                )
            cursor.execute(
                "UPDATE dcim_rackspace SET tenant_id = %s WHERE rack_id IN "
                "(SELECT id FROM dcim_rack WHERE path <@ %s::ltree)",
                [target.pk, new],
            )
            cursor.execute(
                "UPDATE dcim_port SET tenant_id = %s WHERE device_id IN "
                "(SELECT id FROM dcim_device WHERE path <@ %s::ltree)",
                [target.pk, new],
            )
            # Cables inside the site, its VLANs and tagged assignments follow the site.
            in_site = (
                "SELECT p.id FROM dcim_port p JOIN dcim_device d ON d.id = p.device_id"
                " WHERE d.path <@ %s::ltree"
            )
            cursor.execute(
                "UPDATE cabling_cable SET tenant_id = %s, path = "  # noqa: S608  # nosec B608: constant parts
                f"{_REBASE.format(column='path')} WHERE id IN"
                f" (SELECT cable_id FROM cabling_cableend WHERE port_id IN ({in_site}))",
                [target.pk, old, new, new, old, new],
            )
            cursor.execute(
                f"UPDATE cabling_cableend SET tenant_id = %s WHERE port_id IN ({in_site})",  # noqa: S608  # nosec B608
                [target.pk, new],
            )
            # Surveys and planned numbers of the rooms follow the rooms.
            for table in ("locations_roomsurvey", "locations_roomneed"):
                cursor.execute(
                    f"UPDATE {table} SET tenant_id = %s WHERE room_id IN"  # noqa: S608  # nosec B608: fixed names
                    " (SELECT id FROM locations_room WHERE path <@ %s::ltree)",
                    [target.pk, new],
                )
            # Stored configurations follow their devices (scope is the device path).
            cursor.execute(
                "UPDATE configs_deviceconfig c SET tenant_id = %s, path = d.path"
                " FROM dcim_device d WHERE d.id = c.device_id AND d.path <@ %s::ltree",
                [target.pk, new],
            )
            cursor.execute(
                "UPDATE networks_vlan SET tenant_id = %s WHERE site_id = %s", [target.pk, site.pk]
            )
            # Switching and routing records follow their devices, stacks their site.
            cursor.execute(
                "UPDATE networks_stack SET tenant_id = %s WHERE site_id = %s", [target.pk, site.pk]
            )
            for table in (
                "networks_switchsettings",
                "networks_mstinstance",
                "networks_staticroute",
                "networks_bgpneighbor",
                "networks_portprofile",
                "networks_stackmember",
            ):
                cursor.execute(
                    f"UPDATE {table} SET tenant_id = %s WHERE device_id IN"  # noqa: S608  # nosec B608: fixed names
                    " (SELECT id FROM dcim_device WHERE path <@ %s::ltree)",
                    [target.pk, new],
                )
            cursor.execute(
                f"UPDATE networks_taggedvlan SET tenant_id = %s WHERE port_id IN ({in_site})",  # noqa: S608  # nosec B608
                [target.pk, new],
            )
            # Photos and notes follow their objects.
            targets = " UNION ".join(
                f"SELECT id FROM {t} WHERE path <@ %s::ltree"  # noqa: S608  # nosec B608: fixed names
                for t in _TREE_TABLES
            )
            ports = (
                "SELECT p.id FROM dcim_port p JOIN dcim_device d ON d.id = p.device_id"
                " WHERE d.path <@ %s::ltree"
            )
            surveys = (
                "SELECT s.id FROM locations_roomsurvey s JOIN locations_room r ON r.id = s.room_id"
                " WHERE r.path <@ %s::ltree"
            )
            for table in ("metadata_attachment", "metadata_note"):
                cursor.execute(
                    f"UPDATE {table} SET tenant_id = %s WHERE tenant_id = %s"  # noqa: S608  # nosec B608: fixed names
                    f" AND object_id IN ({targets} UNION {ports} UNION {surveys})",
                    [target.pk, source_tenant, *([new] * len(_TREE_TABLES)), new, new],
                )
        Assignment.objects.filter(scope_path__descendant_of=old).update(tenant=target)
        with connection.cursor() as cursor:
            cursor.execute(
                f"UPDATE accounts_assignment SET scope_path = {_REBASE.format(column='scope_path')}"  # noqa: S608  # nosec B608: constant fragment
                " WHERE scope_path <@ %s::ltree",
                [old, new, new, old, old],
            )
    policy.bump_version()
    audit.record(
        "locations.site.moved",
        request=request,
        obj=site,
        tenant_id=target.pk,
        data={"from": str(source_tenant), "to": str(target.pk)},
    )


def validate_message(exc: ValidationError) -> str:
    """Flatten a model validation error for display."""
    return "; ".join(exc.messages)
