"""Vendors, device types and port templates."""

from typing import Any

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError
from django.http import HttpRequest
from django.utils.translation import gettext as _

from patchmanager.accounts.models import User
from patchmanager.accounts.services import ServiceError
from patchmanager.accounts.services.users import require
from patchmanager.audit import services as audit
from patchmanager.library.models import DeviceType, PortTemplate, Vendor
from patchmanager.tenancy.models import Tenant


def _save(obj: Any) -> None:
    try:
        obj.full_clean()
    except ValidationError as exc:
        raise ServiceError("; ".join(exc.messages)) from exc
    try:
        with transaction.atomic():
            obj.save()
    except IntegrityError as exc:
        raise ServiceError(_("An entry with this name already exists.")) from exc


def get_or_create_vendor(request: HttpRequest, actor: User, name: str) -> Vendor:
    """Find a vendor by name (case insensitive) or create it."""
    vendor = Vendor.objects.filter(name__iexact=name.strip()).first()
    if vendor is not None:
        return vendor
    require(actor, "library.vendor.add")
    vendor = Vendor(name=name.strip())
    _save(vendor)
    audit.record("library.vendor.created", request=request, obj=vendor)
    return vendor


def update_vendor(request: HttpRequest, actor: User, vendor: Vendor, **fields: Any) -> Vendor:
    """Rename a vendor or change its website."""
    require(actor, "library.vendor.change", vendor)
    for key, value in fields.items():
        setattr(vendor, key, value)
    _save(vendor)
    audit.record("library.vendor.changed", request=request, obj=vendor)
    return vendor


def delete_vendor(request: HttpRequest, actor: User, vendor: Vendor) -> None:
    """Delete a vendor without device types."""
    require(actor, "library.vendor.delete", vendor)
    try:
        with transaction.atomic():
            vendor.delete()
    except ProtectedError as exc:
        raise ServiceError(_("This vendor still has device types.")) from exc
    audit.record("library.vendor.deleted", request=request, data={"name": vendor.name})


def _scope(tenant: Tenant | None) -> str | None:
    return tenant.policy_path if tenant else None


def create_type(
    request: HttpRequest, actor: User, *, tenant: Tenant | None, **fields: Any
) -> DeviceType:
    """Create a device type: shared (``tenant`` None, needs a global grant) or tenant private."""
    require(actor, "library.devicetype.add", scope_path=_scope(tenant))
    device_type = DeviceType(tenant=tenant, **fields)
    _save(device_type)
    audit.record(
        "library.devicetype.created",
        request=request,
        obj=device_type,
        tenant_id=getattr(tenant, "pk", None),
    )
    return device_type


def update_type(
    request: HttpRequest, actor: User, device_type: DeviceType, **fields: Any
) -> DeviceType:
    """Change a device type (authorised on the stored row and on the result)."""
    stored = DeviceType.objects.get(pk=device_type.pk)
    require(actor, "library.devicetype.change", stored)
    in_use = stored.devices.exists()
    shape = (stored.form, stored.u_height, stored.full_depth)
    for key, value in fields.items():
        setattr(stored, key, value)
    require(actor, "library.devicetype.change", stored)
    if in_use and shape != (stored.form, stored.u_height, stored.full_depth):
        raise ServiceError(_("Form, height and depth of a type in use cannot change."))
    _save(stored)
    audit.record(
        "library.devicetype.changed", request=request, obj=stored, tenant_id=stored.tenant_id
    )
    return stored


def delete_type(request: HttpRequest, actor: User, device_type: DeviceType) -> None:
    """Delete an unused device type."""
    require(actor, "library.devicetype.delete", device_type)
    try:
        with transaction.atomic():
            device_type.delete()
    except ProtectedError as exc:
        raise ServiceError(_("Devices of this type exist.")) from exc
    audit.record(
        "library.devicetype.deleted",
        request=request,
        data={"type": str(device_type)},
        tenant_id=device_type.tenant_id,
    )


def add_port_template(
    request: HttpRequest, actor: User, device_type: DeviceType, **fields: Any
) -> PortTemplate:
    """Add ports to a type. Existing devices keep their ports."""
    require(actor, "library.devicetype.change", device_type)
    rear = fields.get("rear_template")
    if rear is not None and (rear.device_type_id != device_type.pk or rear.kind != "rear"):
        raise ServiceError(_("Front ports can only map to rear ports of the same type."))
    template = PortTemplate(
        device_type=device_type, order=device_type.port_templates.count(), **fields
    )
    if template.kind == "front" and rear is not None and template.count % rear.count:
        raise ServiceError(_("The number of front ports must be a multiple of the rear ports."))
    _save(template)
    audit.record(
        "library.devicetype.ports_added",
        request=request,
        obj=device_type,
        tenant_id=device_type.tenant_id,
        data={"pattern": template.name_pattern, "count": template.count},
    )
    return template


def remove_port_template(request: HttpRequest, actor: User, template: PortTemplate) -> None:
    """Remove a port template."""
    require(actor, "library.devicetype.change", template.device_type)
    template.delete()
    audit.record(
        "library.devicetype.ports_removed",
        request=request,
        obj=template.device_type,
        tenant_id=template.tenant_id,
        data={"pattern": template.name_pattern},
    )


def clone_type(
    request: HttpRequest, actor: User, source: DeviceType, *, tenant: Tenant | None, model: str
) -> DeviceType:
    """Copy a type with its port templates, for example to customise a shared one."""
    copy = create_type(
        request,
        actor,
        tenant=tenant,
        vendor=source.vendor,
        model=model,
        form=source.form,
        category=source.category,
        u_height=source.u_height,
        full_depth=source.full_depth,
        name_pattern=source.name_pattern,
        part_number=source.part_number,
        description=source.description,
    )
    mapping: dict[Any, PortTemplate] = {}
    for template in sorted(source.port_templates.all(), key=lambda t: t.kind != "rear"):
        clone = PortTemplate(
            device_type=copy,
            name_pattern=template.name_pattern,
            kind=template.kind,
            connector=template.connector,
            speed_mbps=template.speed_mbps,
            count=template.count,
            start=template.start,
            positions=template.positions,
            order=template.order,
            rear_template=mapping.get(template.rear_template_id),
        )
        clone.save()
        mapping[template.pk] = clone
    return copy
