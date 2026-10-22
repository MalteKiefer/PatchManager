"""Put documented objects into systems or assign them to organisations."""

from typing import Any

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import NoReverseMatch, reverse
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views.decorators.http import require_http_methods, require_POST

from patchmanager.accounts import policy
from patchmanager.accounts.models import User
from patchmanager.accounts.services import PermissionDeniedError, ServiceError
from patchmanager.audit import services as audit
from patchmanager.core.utils import parse_uuid
from patchmanager.directory.models import Link, Organization, System

# Objects that can be linked, with the permission type that governs them.
TARGETS = {
    "locations.site": "locations.site",
    "locations.building": "locations.building",
    "locations.floor": "locations.floor",
    "locations.room": "locations.room",
    "dcim.rack": "dcim.rack",
    "dcim.device": "dcim.device",
    "configs.deviceconfig": "configs.config",
    "networks.vlan": "networks.vlan",
    "networks.wlan": "networks.wlan",
    "networks.stack": "dcim.device",
    "ipam.vrf": "ipam.vrf",
    "ipam.prefix": "ipam.prefix",
    "ipam.ipaddress": "ipam.ipaddress",
    "ipam.dhcpscope": "ipam.dhcpscope",
    "ipam.dnszone": "ipam.dnszone",
    "circuits.circuit": "circuits.circuit",
    "compute.cluster": "compute.cluster",
    "compute.virtualmachine": "compute.vm",
    "compute.storagevolume": "compute.storage",
}


def _perm(target: Any, action: str) -> str:
    return f"{TARGETS[target._meta.label_lower]}.{action}"


def url(obj: Any) -> str:
    """Detail page of a linked object."""
    from patchmanager.core import crud
    from patchmanager.locations.views import node_url

    label = obj._meta.label_lower
    try:
        if label.startswith("locations.") or label in ("dcim.rack", "dcim.device"):
            return node_url(obj)
        if label == "configs.deviceconfig":
            return reverse("configs:version", args=[obj.device_id, obj.version])
        named = {
            "networks.vlan": "networks:vlan",
            "networks.wlan": "networks:wlan",
            "networks.stack": "networks:stack",
            "ipam.vrf": "ipam:vrf",
            "ipam.prefix": "ipam:prefix",
            "ipam.ipaddress": "ipam:address",
        }
        if label in named:
            return reverse(named[label], args=[obj.pk])
        for resource in crud.REGISTRY.values():
            if resource.model is type(obj):
                return reverse("objects:detail", args=[resource.key, obj.pk])
    except NoReverseMatch:
        pass
    return ""


def resolve(label: str, object_id: Any) -> Any:
    """An allowed target by label and id (Row Level Security applies)."""
    if label not in TARGETS:
        raise Http404
    app_label, model = label.split(".")
    model_class = ContentType.objects.get_by_natural_key(app_label, model).model_class()
    target = (
        model_class._default_manager.filter(pk=parse_uuid(object_id)).first()
        if model_class
        else None
    )
    if target is None:
        raise Http404
    return target


def for_target(user: Any, target: Any) -> list[Link]:
    """Links of ``target`` whose system or organisation the user may see."""
    content_type = ContentType.objects.get_for_model(target)
    rows = Link.objects.filter(content_type=content_type, object_id=target.pk).select_related(
        "organization", "system"
    )
    return [link for link in rows if policy.has_perm(user, *_owner_perm(link))]


def _owner_perm(link: Link) -> tuple[str, Any]:
    owner: Any = link.system or link.organization
    return f"{owner.policy_object_type}.view", owner


def members(user: Any, owner: Organization | System) -> list[dict[str, Any]]:
    """Objects linked to a system or organisation that the user may see, with their pages."""
    field = "system" if isinstance(owner, System) else "organization"
    result = []
    for link in Link.objects.filter(**{field: owner}).select_related("content_type"):
        model: Any = link.content_type.model_class()
        obj = model._default_manager.filter(pk=link.object_id).first() if model else None
        if obj is None or not policy.has_perm(user, _perm(obj, "view"), obj):
            continue
        result.append(
            {
                "link": link,
                "obj": obj,
                "kind": model._meta.verbose_name,
                "url": url(obj),
            }
        )
    return sorted(result, key=lambda r: (str(r["kind"]), str(r["obj"])))


def add(
    request: HttpRequest,
    actor: User,
    target: Any,
    *,
    role: str,
    organization: Organization | None = None,
    system: System | None = None,
) -> Link:
    """Link ``target`` (the user must be allowed to change it and see the owner)."""
    if not policy.has_perm(actor, _perm(target, "change"), target):
        raise PermissionDeniedError(_("You do not have permission to do that."))
    owner = system or organization
    if owner is None or not policy.has_perm(actor, f"{owner.policy_object_type}.view", owner):
        raise PermissionDeniedError(_("You do not have permission to do that."))
    if owner.tenant_id != target.tenant_id:
        raise ServiceError(_("Both must belong to the same tenant."))
    link = Link(
        tenant_id=target.tenant_id,
        created_by=actor,
        content_type=ContentType.objects.get_for_model(target),
        object_id=target.pk,
        organization=organization,
        system=system,
        role=role,
    )
    try:
        with transaction.atomic():
            link.full_clean(exclude=["tenant"])
            link.save()
    except IntegrityError as exc:
        raise ServiceError(_("This link already exists.")) from exc
    audit.record(
        "directory.link.created", request=request, obj=target, data={"to": str(owner), "role": role}
    )
    return link


def remove(request: HttpRequest, actor: User, link: Link) -> None:
    """Remove a link (needs change permission on the linked object)."""
    model = link.content_type.model_class()
    target = model._default_manager.filter(pk=link.object_id).first() if model else None
    if target is None or not policy.has_perm(actor, _perm(target, "change"), target):
        raise PermissionDeniedError(_("You do not have permission to do that."))
    audit.record(
        "directory.link.deleted",
        request=request,
        obj=target,
        data={"to": str(link.system or link.organization)},
    )
    link.delete()


# --- Pages ---------------------------------------------------------------------


class LinkForm(forms.Form):
    """A system, or an organisation with a role."""

    system = forms.ModelChoiceField(
        label=gettext_lazy("System"), queryset=System.objects.none(), required=False
    )
    organization = forms.ModelChoiceField(
        label=gettext_lazy("or organization"), queryset=Organization.objects.none(), required=False
    )
    role = forms.ChoiceField(label=gettext_lazy("Role"), choices=Link.Role.choices)

    def __init__(self, *args: Any, user: Any, tenant_id: Any, **kwargs: Any) -> None:
        """Systems and organisations of the target's tenant the user may see."""
        super().__init__(*args, **kwargs)
        for name, model in (("system", System), ("organization", Organization)):
            rows = [
                o
                for o in model.objects.filter(tenant_id=tenant_id)
                if policy.has_perm(user, f"{o.policy_object_type}.view", o)
            ]
            self.fields[name].queryset = model.objects.filter(pk__in=[o.pk for o in rows])  # type: ignore[attr-defined]

    def clean(self) -> dict[str, Any]:
        """Exactly one of system and organisation; systems take the role "part of"."""
        data = super().clean() or {}
        if bool(data.get("system")) == bool(data.get("organization")):
            raise forms.ValidationError(_("Choose a system or an organization."))
        if data.get("system"):
            data["role"] = Link.Role.PART
        elif data.get("role") == Link.Role.PART:
            raise forms.ValidationError(_("Choose the role of the organization."))
        return data


def _actor(request: HttpRequest) -> User:
    assert isinstance(request.user, User)  # noqa: S101  # nosec B101: views require login
    return request.user


@login_required
@require_http_methods(["GET", "POST"])
def link_create(request: HttpRequest) -> HttpResponse:
    """Page that links an object (``?target=label:id``) to a system or organisation."""
    label, _sep, object_id = (
        request.POST.get("target") or request.GET.get("target", "")
    ).partition(":")
    target = resolve(label, object_id)
    actor = _actor(request)
    if not policy.has_perm(actor, _perm(target, "view"), target):
        raise Http404
    if not policy.has_perm(actor, _perm(target, "change"), target):
        raise PermissionDenied
    back = url(target)
    form = LinkForm(request.POST or None, user=actor, tenant_id=target.tenant_id)
    if request.method == "POST" and form.is_valid():
        try:
            add(
                request,
                actor,
                target,
                role=form.cleaned_data["role"],
                organization=form.cleaned_data.get("organization"),
                system=form.cleaned_data.get("system"),
            )
        except PermissionDeniedError as exc:
            raise PermissionDenied from exc
        except ServiceError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, _("Linked."))
            return redirect(back or "/")
    return render(
        request,
        "core/create.html",
        {
            "title": _("Link %(object)s") % {"object": target},
            "intro": _(
                "Put it into a system, or assign it to an organization, for example a customer."
            ),
            "form": form,
            "back_url": back,
            "hidden": {"target": f"{label}:{target.pk}"},
            "submit": _("Link"),
        },
    )


@login_required
@require_POST
def link_delete(request: HttpRequest, pk: str) -> HttpResponse:
    """Remove a link and return to the page of the linked object."""
    link = get_object_or_404(Link.objects.select_related("content_type"), pk=pk)
    model: Any = link.content_type.model_class()
    target = model._default_manager.filter(pk=link.object_id).first() if model else None
    try:
        remove(request, _actor(request), link)
    except PermissionDeniedError as exc:
        raise PermissionDenied from exc
    messages.success(request, _("Link removed."))
    return redirect((url(target) if target is not None else "") or "/")
