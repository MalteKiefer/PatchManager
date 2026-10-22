"""Map, site list and one generic detail page per level of the location tree."""

from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Model, Prefetch
from django.forms import ModelForm
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views.decorators.http import require_http_methods

from patchmanager.accounts import policy
from patchmanager.accounts.models import User
from patchmanager.accounts.services import ServiceError
from patchmanager.core.hierarchy import HierarchyNode
from patchmanager.dcim.models import Rack
from patchmanager.locations import services
from patchmanager.locations.forms import (
    BuildingForm,
    FloorForm,
    MoveSiteForm,
    RackForm,
    RoomForm,
    SiteForm,
)
from patchmanager.locations.models import Building, Floor, Room, Site
from patchmanager.tenancy.models import Tenant


@dataclass(frozen=True)
class Level:
    """One level of the tree: its model and form, and what it contains."""

    model: type[HierarchyNode]
    form: type[ModelForm]  # type: ignore[type-arg]
    child_model: type[HierarchyNode] | None
    child_form: type[ModelForm] | None  # type: ignore[type-arg]
    child_label: Any
    new_child: Any = None  # title of the page that creates a child


LEVELS: dict[str, Level] = {
    "site": Level(
        Site,
        SiteForm,
        Building,
        BuildingForm,
        gettext_lazy("Buildings"),
        gettext_lazy("New building"),
    ),
    "building": Level(
        Building, BuildingForm, Floor, FloorForm, gettext_lazy("Floors"), gettext_lazy("New floor")
    ),
    "floor": Level(
        Floor, FloorForm, Room, RoomForm, gettext_lazy("Rooms"), gettext_lazy("New room")
    ),
    "room": Level(Room, RoomForm, Rack, RackForm, gettext_lazy("Racks"), gettext_lazy("New rack")),
}


def node_url(node: Any) -> str:
    """Detail URL of any tree node (including devices)."""
    if isinstance(node, Rack):
        return reverse("dcim:rack", args=[node.pk])
    if type(node).__name__ == "Device":
        return reverse("devices:device", args=[node.pk])
    return reverse("locations:node", args=[type(node).__name__.lower(), node.pk])


def _actor(request: HttpRequest) -> User:
    assert isinstance(request.user, User)  # noqa: S101  # nosec B101: views require login
    return request.user


def _visible(request: HttpRequest, model: type[Model], **filters: Any) -> Any:
    """Rows of ``model`` the actor may view (RLS + policy)."""
    perm = f"{model.policy_object_type}.view"  # type: ignore[attr-defined]
    return policy.scope_queryset(request.user, perm, model.objects.filter(**filters))  # type: ignore[attr-defined]


def _sites(request: HttpRequest) -> Any:
    tenant = getattr(request, "tenant", None)
    return _visible(request, Site, tenant=tenant) if tenant else Site.objects.none()


@login_required
def index(request: HttpRequest) -> HttpResponse:
    """Map and list of the current tenant's sites."""
    tenant = getattr(request, "tenant", None)
    sites = list(_sites(request))
    markers = [
        {"name": s.name, "url": node_url(s), "lat": float(s.latitude), "lng": float(s.longitude)}
        for s in sites
        if s.latitude is not None
    ]
    can_add = tenant is not None and policy.has_perm(request.user, "locations.site.add", tenant)
    return render(
        request,
        "locations/index.html",
        {
            "sites": sites,
            "markers": markers,
            "can_add": can_add,
            "map_tiles": settings.MAP_TILE_URL,
            "map_attribution": settings.MAP_ATTRIBUTION,
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def site_create(request: HttpRequest) -> HttpResponse:
    """New site in the current tenant."""
    tenant = getattr(request, "tenant", None)
    if tenant is None or not policy.has_perm(request.user, "locations.site.add", tenant):
        raise PermissionDenied
    form = SiteForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            site = services.create(
                request, _actor(request), Site, parent=tenant, **form.cleaned_data
            )
        except ServiceError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, _("Site created."))
            return redirect(node_url(site))
    return render(
        request,
        "locations/site_form.html",
        {
            "form": form,
            "map_tiles": settings.MAP_TILE_URL,
            "map_attribution": settings.MAP_ATTRIBUTION,
        },
    )


def _tree(request: HttpRequest, site: Site) -> list[Building]:
    """Buildings with floors and rooms the actor may see, for one site."""
    rooms = _visible(request, Room, path__descendant_of=site.path)
    floors = _visible(request, Floor, path__descendant_of=site.path).prefetch_related(
        Prefetch("rooms", queryset=rooms, to_attr="visible_rooms")
    )
    buildings = _visible(request, Building, site=site).prefetch_related(
        Prefetch("floors", queryset=floors, to_attr="visible_floors")
    )
    return list(buildings)


@login_required
@require_http_methods(["GET", "POST"])
def room_plan(request: HttpRequest, pk: str) -> HttpResponse:
    """Page that sets a room's own planned numbers per device category."""
    from patchmanager.library.models import DeviceCategory
    from patchmanager.locations import rooms

    room: Any = get_object_or_404(Room.objects.select_related("kind"), pk=pk)
    actor = _actor(request)
    if not policy.has_perm(actor, "locations.room.view", room):
        raise Http404
    if not policy.has_perm(actor, "locations.room.change", room):
        raise PermissionDenied
    defaults = {n.category_id: n.count for n in room.kind.needs.all()}
    own = {n.category_id: n.count for n in room.needs.all()}
    categories = list(DeviceCategory.objects.all())
    errors = []
    if request.method == "POST":
        counts: dict[str, int | None] = {}
        for c in categories:
            raw = request.POST.get(f"need-{c.key}", "").strip()
            if not raw:
                counts[c.key] = None
            elif raw.isdecimal() and int(raw) <= 999:
                counts[c.key] = int(raw)
            else:
                errors.append(c.label)
        if not errors:
            rooms.set_needs(request, actor, room, counts)
            messages.success(request, _("Saved."))
            return redirect(node_url(room))
    return render(
        request,
        "locations/room_plan.html",
        {
            "room": room,
            "rows": [(c, own.get(c.key), defaults.get(c.key)) for c in categories],
            "errors": errors,
            "breadcrumbs": [(a, node_url(a)) for a in [*room.ancestors(), room]],
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def node_create(request: HttpRequest, level: str, pk: str) -> HttpResponse:
    """Page that creates a child of a node (building, floor, room or rack)."""
    config = LEVELS.get(level)
    if config is None or config.child_model is None or config.child_form is None:
        raise Http404
    node: Any = get_object_or_404(config.model, pk=pk)
    actor = _actor(request)
    if not policy.has_perm(actor, f"{node.policy_object_type}.view", node):
        raise Http404
    if not policy.has_perm(actor, f"{config.child_model.policy_object_type}.add", node):
        raise PermissionDenied
    form = config.child_form(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            child = services.create(
                request, actor, config.child_model, parent=node, **form.cleaned_data
            )
        except ServiceError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, _("Created."))
            return redirect(node_url(child))
    return render(
        request,
        "core/create.html",
        {
            "title": config.new_child,
            "form": form,
            "back_url": node_url(node),
            "breadcrumbs": [(a, node_url(a)) for a in [*node.ancestors(), node]],
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def node_detail(request: HttpRequest, level: str, pk: str) -> HttpResponse:
    """Show a node with its children; edit it, add children, delete it."""
    if level not in LEVELS:
        raise Http404
    config = LEVELS[level]
    node: Any = get_object_or_404(config.model, pk=pk)  # RLS hides other tenants
    actor = _actor(request)
    if not policy.has_perm(actor, f"{node.policy_object_type}.view", node):
        raise Http404
    can_change = policy.has_perm(actor, f"{node.policy_object_type}.change", node)
    can_delete = policy.has_perm(actor, f"{node.policy_object_type}.delete", node)
    child_type = config.child_model.policy_object_type if config.child_model else None
    can_add = bool(child_type) and policy.has_perm(actor, f"{child_type}.add", node)
    action = request.POST.get("action")
    form = config.form(request.POST if action == "edit" else None, instance=node)

    if request.method == "POST":
        try:
            if action == "edit" and form.is_valid():
                services.update(request, actor, node, **form.cleaned_data)
                messages.success(request, _("Saved."))
                return redirect(node_url(node))
            if action == "delete":
                parent = node.parent
                services.delete(request, actor, node)
                messages.success(request, _("Deleted."))
                return redirect(node_url(parent) if parent else reverse("locations:index"))
        except ServiceError as exc:
            messages.error(request, str(exc))
            if action != "edit":
                return redirect(node_url(node))
            form.add_error(None, str(exc))

    children: Any = []
    if config.child_model is not None:
        children = _visible(
            request, config.child_model, **{str(config.child_model.parent_field): node}
        )
    context = {
        "level": level,
        "node": node,
        "form": form,
        "new_child": config.new_child,
        "children": children,
        "child_label": config.child_label,
        "can_change": can_change,
        "can_delete": can_delete,
        "can_add": can_add,
        "breadcrumbs": [(a, node_url(a)) for a in node.ancestors()],
        "tree": _tree(request, node) if level == "site" else None,
        "can_move": level == "site" and policy.has_perm(actor, "locations.site.move_tenant", node),
        "map_tiles": settings.MAP_TILE_URL,
        "map_attribution": settings.MAP_ATTRIBUTION,
        "site_marker": [
            {
                "name": node.name,
                "url": node_url(node),
                "lat": float(node.latitude),
                "lng": float(node.longitude),
            }
        ]
        if level == "site" and node.latitude is not None
        else [],
    }
    if level == "room":
        from patchmanager.dcim.models import Device

        context["children"] = list(children.prefetch_related("spaces"))
        context["floor_devices"] = _visible(
            request, Device, room=node, rack__isnull=True
        ).select_related("device_type__vendor")
        context["can_add_device"] = policy.has_perm(actor, "dcim.device.add", node)
        from patchmanager.locations import rooms

        context["plan"] = rooms.plan(actor, node)
        context["outlets"] = rooms.cabling(actor, node)
    return render(request, "locations/node.html", context)


@login_required
@require_http_methods(["GET", "POST"])
def site_move(request: HttpRequest, pk: str) -> HttpResponse:
    """Move a site and its contents to another tenant."""
    site = get_object_or_404(Site, pk=pk)
    actor = _actor(request)
    if not policy.has_perm(actor, "locations.site.move_tenant", site):
        raise PermissionDenied
    targets = Tenant.objects.filter(archived_at__isnull=True).exclude(pk=site.tenant_id)
    allowed = [t.pk for t in targets if policy.has_perm(actor, "locations.site.add", t)]
    form = MoveSiteForm(request.POST or None, tenants=targets.filter(pk__in=allowed))
    if request.method == "POST" and form.is_valid():
        if form.cleaned_data["confirm"] != site.name:
            form.add_error("confirm", _("The name does not match."))
        else:
            try:
                services.move_site(request, actor, site, form.cleaned_data["tenant"])
            except ServiceError as exc:
                form.add_error(None, str(exc))
            else:
                request.session["_pm_tenant"] = str(form.cleaned_data["tenant"].pk)
                messages.success(request, _("Site moved."))
                return redirect(node_url(Site.objects.get(pk=site.pk)))
    return render(request, "locations/site_move.html", {"site": site, "form": form})
