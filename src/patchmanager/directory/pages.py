"""Pages of organisations, contacts and systems (through the shared record pages)."""

from typing import Any

from django import forms
from django.http import HttpRequest
from django.utils.translation import gettext_lazy as _

from patchmanager.accounts import policy
from patchmanager.core import crud
from patchmanager.directory import links
from patchmanager.directory.models import Contact, Organization, System


def visible(actor: Any, queryset: Any) -> Any:
    """Rows of ``queryset`` the actor may view (by path where the table has one)."""
    model = queryset.model
    permission = f"{model.policy_object_type}.view"
    if any(f.name == "path" for f in model._meta.concrete_fields):
        return policy.scope_queryset(actor, permission, queryset)
    allowed = [o.pk for o in queryset if policy.has_perm(actor, permission, o)]
    return queryset.filter(pk__in=allowed)


class TenantForm(forms.ModelForm):  # type: ignore[type-arg]
    """A record form whose choices are limited to the record's tenant."""

    tenant_fields: tuple[str, ...] = ()

    def __init__(self, *args: Any, tenant: Any, actor: Any, **kwargs: Any) -> None:
        """Offer only records of the tenant that the user may see."""
        super().__init__(*args, **kwargs)
        for name in self.tenant_fields:
            if name in self.fields:
                field: Any = self.fields[name]
                field.queryset = visible(actor, field.queryset.filter(tenant=tenant))


class OrganizationForm(TenantForm):
    """Name, kind and contact details of an organisation."""

    class Meta:
        model = Organization
        fields = ["name", "kind", "number", "website", "phone", "email", "address", "notes"]
        widgets = {
            "address": forms.Textarea(attrs={"rows": 3}),
            "notes": forms.Textarea(attrs={"rows": 3}),
        }


class ContactForm(TenantForm):
    """A person at an organisation."""

    tenant_fields = ("organization",)

    class Meta:
        model = Contact
        fields = ["organization", "name", "role", "email", "phone", "notes"]
        widgets = {"notes": forms.Textarea(attrs={"rows": 3})}


class SystemForm(TenantForm):
    """A system with its customer, site and contact."""

    tenant_fields = ("customer", "site", "contact")

    class Meta:
        model = System
        fields = ["name", "kind", "customer", "site", "criticality", "contact", "description"]
        widgets = {"description": forms.Textarea(attrs={"rows": 3})}


def _members(request: HttpRequest, obj: Any) -> dict[str, Any]:
    return {"members": links.members(request.user, obj)}


crud.register(
    crud.Resource(
        key="organizations",
        model=Organization,
        form=OrganizationForm,
        title=_("Organizations"),
        new=_("New organization"),
        group="directory",
        columns=(
            (_("Name"), "name"),
            (_("Kind"), "kind"),
            (_("Number"), "number"),
            (_("Phone"), "phone"),
        ),
        children=("contacts",),
        context=_members,
        template="directory/_members.html",
    )
)
crud.register(
    crud.Resource(
        key="contacts",
        model=Contact,
        form=ContactForm,
        title=_("Contacts"),
        new=_("New contact"),
        group="directory",
        columns=(
            (_("Name"), "name"),
            (_("Role"), "role"),
            (_("Email"), "email"),
            (_("Phone"), "phone"),
        ),
        parent=("organization", "organizations"),
        hidden=True,
    )
)
crud.register(
    crud.Resource(
        key="systems",
        model=System,
        form=SystemForm,
        title=_("Systems"),
        new=_("New system"),
        group="directory",
        columns=(
            (_("Name"), "name"),
            (_("Kind"), "kind"),
            (_("Customer"), "customer"),
            (_("Site"), "site"),
            (_("Criticality"), "criticality"),
        ),
        context=_members,
        template="directory/_members.html",
    )
)
