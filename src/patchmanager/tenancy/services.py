"""Tenant lifecycle. Callers pass the acting user; checks run through the policy."""

from django.db import IntegrityError, transaction
from django.http import HttpRequest
from django.utils import timezone
from django.utils.translation import gettext as _

from patchmanager.accounts.models import User
from patchmanager.accounts.services import ServiceError
from patchmanager.accounts.services.users import require
from patchmanager.audit import services as audit
from patchmanager.tenancy.models import Tenant

FIELDS = ("slug", "name", "address", "contact_name", "contact_email", "requires_mfa")


def create_tenant(request: HttpRequest, actor: User, **values: object) -> Tenant:
    """Create a tenant."""
    require(actor, "tenancy.tenant.add")
    from patchmanager.instance import editions

    editions.check_limit("tenants")
    tenant = Tenant(**{k: v for k, v in values.items() if k in FIELDS})
    tenant.full_clean()
    try:
        with transaction.atomic():
            tenant.save()
    except IntegrityError as exc:
        raise ServiceError(_("A tenant with this identifier already exists.")) from exc
    audit.record("tenancy.tenant.created", request=request, obj=tenant, tenant_id=tenant.pk)
    return tenant


def update_tenant(request: HttpRequest, actor: User, tenant: Tenant, **values: object) -> Tenant:
    """Change tenant details."""
    require(actor, "tenancy.tenant.change", tenant)
    before = {f: getattr(tenant, f) for f in FIELDS}
    for key, value in values.items():
        if key in FIELDS:
            setattr(tenant, key, value)
    tenant.full_clean()
    try:
        with transaction.atomic():
            tenant.save()
    except IntegrityError as exc:
        raise ServiceError(_("A tenant with this identifier already exists.")) from exc
    after = {f: getattr(tenant, f) for f in FIELDS}
    changed = {k: [before[k], after[k]] for k in FIELDS if before[k] != after[k]}
    if "requires_mfa" in changed:
        from patchmanager.accounts import policy

        policy.bump_version()  # grants of API clients depend on it
    audit.record(
        "tenancy.tenant.changed",
        request=request,
        obj=tenant,
        tenant_id=tenant.pk,
        data={"changed": changed},
    )
    return tenant


def set_archived(request: HttpRequest, actor: User, tenant: Tenant, *, archived: bool) -> None:
    """Archive (hide, keep data) or restore a tenant."""
    require(actor, "tenancy.tenant.change", tenant)
    tenant.archived_at = timezone.now() if archived else None
    tenant.save(update_fields=["archived_at", "updated_at"])
    action = "tenancy.tenant.archived" if archived else "tenancy.tenant.restored"
    audit.record(action, request=request, obj=tenant, tenant_id=tenant.pk)


def delete_tenant(request: HttpRequest, actor: User, tenant: Tenant, *, confirm_slug: str) -> None:
    """Delete an archived tenant after the identifier was typed again."""
    require(actor, "tenancy.tenant.delete", tenant)
    if tenant.archived_at is None:
        raise ServiceError(_("Archive the tenant before deleting it."))
    if confirm_slug != tenant.slug:
        raise ServiceError(_("The identifier does not match."))
    audit.record("tenancy.tenant.deleted", request=request, obj=tenant, tenant_id=tenant.pk)
    from patchmanager.history.models import Change

    pk = tenant.pk
    with transaction.atomic():
        tenant.delete()
        Change.objects.filter(tenant_id=pk).delete()  # the triggers recorded the cascade too
