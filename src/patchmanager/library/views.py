"""Device library: browse types, edit templates and their ports, import and export."""

from typing import Any

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Q
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views.decorators.http import require_http_methods, require_POST

from patchmanager.accounts import policy
from patchmanager.accounts.models import User
from patchmanager.accounts.services import ServiceError
from patchmanager.core import lists
from patchmanager.core.utils import label_choices, parse_uuid
from patchmanager.library import catalog, interchange, services
from patchmanager.library.models import CatalogType, DeviceType, PortTemplate
from patchmanager.metadata.models import Attachment

TYPE_FIELDS = [
    "model",
    "form",
    "category",
    "u_height",
    "full_depth",
    "mounting",
    "airflow",
    "power_supplies",
    "max_power_w",
    "weight_kg",
    "name_pattern",
    "part_number",
    "description",
]


class DeviceTypeForm(forms.ModelForm):  # type: ignore[type-arg]
    """Device type details; the vendor is typed as a name."""

    vendor_name = forms.CharField(label=gettext_lazy("Vendor"), max_length=100)
    shared = forms.BooleanField(label=gettext_lazy("Shared with all tenants"), required=False)

    class Meta:
        model = DeviceType
        fields = TYPE_FIELDS
        widgets = {
            "description": forms.Textarea(attrs={"rows": 2}),
            # Height and depth only matter in a rack (app.js hides them otherwise).
            "u_height": forms.NumberInput(attrs={"data-show-if": "form=rack"}),
            "full_depth": forms.CheckboxInput(attrs={"data-show-if": "form=rack"}),
        }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Categories sorted by name; the height is optional for devices outside racks."""
        super().__init__(*args, **kwargs)
        label_choices(self.fields["category"])
        self.fields["u_height"].required = False

    def clean(self) -> dict[str, Any]:
        """Devices outside racks take no rack units; rack devices need at least one."""
        data = super().clean() or {}
        if data.get("form") == DeviceType.Form.RACK:
            if not data.get("u_height"):
                self.add_error("u_height", gettext_lazy("Rack mounted devices need a height."))
        else:
            data["u_height"] = 0
            data["full_depth"] = False
        return data


class PortTemplateForm(forms.ModelForm):  # type: ignore[type-arg]
    """A group of ports."""

    class Meta:
        model = PortTemplate
        fields = [
            "name_pattern",
            "kind",
            "connector",
            "speed_mbps",
            "count",
            "start",
            "positions",
            "rear_template",
        ]

    def __init__(self, *args: Any, device_type: DeviceType, **kwargs: Any) -> None:
        """Offer only rear port templates of this type as mapping targets."""
        super().__init__(*args, **kwargs)
        self.fields["rear_template"].queryset = device_type.port_templates.filter(kind="rear")  # type: ignore[attr-defined]


class ImagesForm(forms.Form):
    """Pick front and rear images among the type's attached images."""

    front_image = forms.ModelChoiceField(
        label=gettext_lazy("Front image"), queryset=Attachment.objects.none(), required=False
    )
    rear_image = forms.ModelChoiceField(
        label=gettext_lazy("Rear image"), queryset=Attachment.objects.none(), required=False
    )

    def __init__(self, *args: Any, images: Any, **kwargs: Any) -> None:
        """``images``: image attachments of the type."""
        super().__init__(*args, **kwargs)
        for name in ("front_image", "rear_image"):
            self.fields[name].queryset = images  # type: ignore[attr-defined]


class ImportForm(forms.Form):
    """Device type definition file."""

    file = forms.FileField(label=gettext_lazy("YAML or JSON file"))
    shared = forms.BooleanField(label=gettext_lazy("Shared with all tenants"), required=False)


def _actor(request: HttpRequest) -> User:
    assert isinstance(request.user, User)  # noqa: S101  # nosec B101: views require login
    return request.user


def visible_types(request: HttpRequest) -> Any:
    """Shared types plus those private to the current tenant (RLS adds the outer bound)."""
    tenant = getattr(request, "tenant", None)
    return DeviceType.objects.filter(Q(tenant__isnull=True) | Q(tenant=tenant)).select_related(
        "vendor"
    )


def _scope(request: HttpRequest, shared: bool) -> Any:
    """Tenant for a new type, or ``None`` for shared ones."""
    if shared:
        return None
    tenant = getattr(request, "tenant", None)
    if tenant is None:
        raise PermissionDenied
    return tenant


@login_required
def type_list(request: HttpRequest) -> HttpResponse:
    """Searchable list of device types."""
    query = request.GET.get("q", "").strip()
    form = request.GET.get("form", "")
    types = visible_types(request).annotate(device_count=Count("devices"))
    if query:
        types = types.filter(
            Q(model__icontains=query)
            | Q(vendor__name__icontains=query)
            | Q(part_number__icontains=query)
        )
    if form in DeviceType.Form.values:
        types = types.filter(form=form)
    tenant = getattr(request, "tenant", None)
    can_add = policy.has_perm(request.user, "library.devicetype.add") or (
        tenant is not None and policy.has_perm(request.user, "library.devicetype.add", tenant)
    )
    return render(
        request,
        "library/types.html",
        {
            "types": types,
            "q": query,
            "form_filter": form,
            "forms": DeviceType.Form.choices,
            "can_add": can_add,
            "import_form": ImportForm(),
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def type_create(request: HttpRequest) -> HttpResponse:
    """New (custom) device type."""
    form = DeviceTypeForm(request.POST or None, initial={"form": request.GET.get("form", "rack")})
    lists.link_field(form, "category", request.user, "categories")
    if request.method == "POST" and form.is_valid():
        data = {k: form.cleaned_data[k] for k in TYPE_FIELDS}
        try:
            vendor = services.get_or_create_vendor(
                request, _actor(request), form.cleaned_data["vendor_name"]
            )
            device_type = services.create_type(
                request,
                _actor(request),
                tenant=_scope(request, form.cleaned_data["shared"]),
                vendor=vendor,
                **data,
            )
        except ServiceError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, _("Device type created. Now add its ports."))
            return redirect("library:type", device_type.pk)
    return render(request, "library/type_form.html", {"form": form})


@login_required
@require_http_methods(["GET", "POST"])
def type_ports_create(request: HttpRequest, pk: str) -> HttpResponse:
    """Page that adds ports to a device type."""
    device_type = get_object_or_404(visible_types(request), pk=pk)
    actor = _actor(request)
    if not policy.has_perm(actor, "library.devicetype.change", device_type):
        raise PermissionDenied
    form = PortTemplateForm(request.POST or None, device_type=device_type)
    if request.method == "POST" and form.is_valid():
        try:
            services.add_port_template(request, actor, device_type, **form.cleaned_data)
        except ServiceError as exc:
            form.add_error(None, str(exc))
        else:
            return redirect("library:type", pk)
    return render(
        request,
        "core/create.html",
        {
            "title": _("Add ports to %(type)s") % {"type": device_type},
            "form": form,
            "submit": _("Add"),
            "back_url": reverse("library:type", args=[pk]),
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def type_detail(request: HttpRequest, pk: str) -> HttpResponse:
    """Edit a device type, its ports and images."""
    device_type = get_object_or_404(visible_types(request), pk=pk)
    actor = _actor(request)
    can_change = policy.has_perm(actor, "library.devicetype.change", device_type)
    action = request.POST.get("action")
    images = Attachment.objects.filter(
        content_type__model="devicetype", object_id=device_type.pk, kind="image"
    )
    form = DeviceTypeForm(
        request.POST if action == "edit" else None,
        instance=device_type,
        initial={"vendor_name": device_type.vendor.name, "shared": device_type.tenant_id is None},
    )
    del form.fields["shared"]  # scope is fixed after creation
    lists.link_field(form, "category", request.user, "categories")
    images_form = ImagesForm(
        request.POST if action == "images" else None,
        images=images,
        initial={"front_image": device_type.front_image, "rear_image": device_type.rear_image},
    )
    if request.method == "POST":
        try:
            if action == "edit" and form.is_valid():
                vendor = services.get_or_create_vendor(
                    request, actor, form.cleaned_data["vendor_name"]
                )
                services.update_type(
                    request,
                    actor,
                    device_type,
                    vendor=vendor,
                    **{k: form.cleaned_data[k] for k in TYPE_FIELDS},
                )
                messages.success(request, _("Saved."))
                return redirect("library:type", pk)
            if action == "remove_ports":
                template = get_object_or_404(
                    PortTemplate,
                    pk=parse_uuid(request.POST.get("template")),
                    device_type=device_type,
                )
                services.remove_port_template(request, actor, template)
                return redirect("library:type", pk)
            if action == "images" and images_form.is_valid():
                services.update_type(request, actor, device_type, **images_form.cleaned_data)
                return redirect("library:type", pk)
            if action == "clone":
                copy = services.clone_type(
                    request,
                    actor,
                    device_type,
                    tenant=_scope(request, False),
                    model=f"{device_type.model} ({_('copy')})",
                )
                messages.success(request, _("Copied into a private type of this tenant."))
                return redirect("library:type", copy.pk)
            if action == "delete":
                services.delete_type(request, actor, device_type)
                messages.success(request, _("Deleted."))
                return redirect("library:types")
        except ServiceError as exc:
            messages.error(request, str(exc))
            return redirect("library:type", pk)
    return render(
        request,
        "library/type_detail.html",
        {
            "device_type": device_type,
            "form": form,
            "images_form": images_form,
            "templates": device_type.port_templates.select_related("rear_template"),
            "can_change": can_change,
            "can_delete": policy.has_perm(actor, "library.devicetype.delete", device_type),
            "can_clone": getattr(request, "tenant", None) is not None,
        },
    )


@login_required
def type_export(request: HttpRequest, pk: str) -> HttpResponse:
    """Download a type in the community format."""
    device_type = get_object_or_404(visible_types(request), pk=pk)
    if not policy.has_perm(request.user, "library.devicetype.view", device_type):
        raise Http404
    fmt = "json" if request.GET.get("format") == "json" else "yaml"
    response = HttpResponse(
        interchange.export_type(device_type, fmt=fmt),
        content_type="application/json" if fmt == "json" else "application/yaml",
    )
    response["Content-Disposition"] = f'attachment; filename="device-type.{fmt}"'
    return response


@login_required
@require_POST
def type_import(request: HttpRequest) -> HttpResponse:
    """Create a type from an uploaded definition."""
    form = ImportForm(request.POST, request.FILES)
    if not form.is_valid() or form.cleaned_data["file"].size > 1_000_000:
        messages.error(request, _("Choose a file of at most 1 MB."))
        return redirect("library:types")
    try:
        payload = form.cleaned_data["file"].read().decode("utf-8")
        device_type = interchange.import_type(
            request, _actor(request), payload, tenant=_scope(request, form.cleaned_data["shared"])
        )
    except UnicodeDecodeError:
        messages.error(request, _("The file must be UTF-8 text."))
    except ServiceError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, _("Device type imported."))
        return redirect("library:type", device_type.pk)
    return redirect("library:types")


class AdoptForm(forms.Form):
    """Where a catalog type goes."""

    shared = forms.BooleanField(label=gettext_lazy("Shared with all tenants"), required=False)


@login_required
def catalog_search(request: HttpRequest) -> HttpResponse:
    """Search the bundled device type catalog."""
    query = request.GET.get("q", "").strip()[:100]
    return render(
        request,
        "library/catalog.html",
        {
            "q": query,
            "results": catalog.search(query) if len(query) >= 2 else [],
            "total": CatalogType.objects.count(),
            "source": CatalogType.objects.values_list("commit", flat=True).first() or "",
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def catalog_adopt(request: HttpRequest, pk: int) -> HttpResponse:
    """Preview a catalog type and copy it with its ports into the library."""
    entry = get_object_or_404(CatalogType, pk=pk)
    form = AdoptForm(request.POST or None)
    actor = _actor(request)
    tenant = getattr(request, "tenant", None)
    can_add = policy.has_perm(actor, "library.devicetype.add") or (
        tenant is not None and policy.has_perm(actor, "library.devicetype.add", tenant)
    )
    if request.method == "POST" and form.is_valid():
        try:  # the service checks the permission for the chosen scope
            device_type = catalog.adopt(
                request, actor, entry, tenant=_scope(request, form.cleaned_data["shared"])
            )
        except ServiceError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, _("Added to the library with all its ports."))
            return redirect("library:type", device_type.pk)
    sections = [
        (label, entry.spec.get(key) or [])
        for key, label in (
            ("interfaces", _("Network ports")),
            ("front-ports", _("Front ports")),
            ("rear-ports", _("Rear ports")),
            ("console-ports", _("Console ports")),
            ("power-ports", _("Power inlets")),
        )
    ]
    return render(
        request,
        "library/catalog_entry.html",
        {"entry": entry, "form": form, "sections": sections, "can_add": can_add},
    )
