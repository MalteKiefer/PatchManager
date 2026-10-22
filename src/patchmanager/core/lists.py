"""Lists administrators shape themselves: device categories and room types (with needs)."""

from dataclasses import dataclass
from typing import Any

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views.decorators.http import require_http_methods, require_POST

from patchmanager.accounts import policy
from patchmanager.accounts.models import User
from patchmanager.accounts.services import ServiceError
from patchmanager.accounts.services.users import require
from patchmanager.audit import services as audit
from patchmanager.library.models import DeviceCategory
from patchmanager.locations.models import RoomType, RoomTypeNeed


class CategoryForm(forms.ModelForm):  # type: ignore[type-arg]
    """Category fields."""

    class Meta:
        model = DeviceCategory
        fields = ["key", "name", "glyph", "colour", "order"]
        widgets = {"colour": forms.TextInput(attrs={"type": "color"})}


class RoomTypeForm(forms.ModelForm):  # type: ignore[type-arg]
    """Room type fields."""

    class Meta:
        model = RoomType
        fields = ["key", "name", "description", "order"]
        widgets = {"description": forms.Textarea(attrs={"rows": 2})}


class NeedForm(forms.ModelForm):  # type: ignore[type-arg]
    """A category and how many of it a room of this type needs."""

    class Meta:
        model = RoomTypeNeed
        fields = ["category", "count"]


@dataclass(frozen=True)
class Kind:
    """One editable list."""

    slug: str
    model: Any
    form: Any
    permission: str  # object type
    title: Any
    new: Any


KINDS = {
    k.slug: k
    for k in (
        Kind(
            "categories",
            DeviceCategory,
            CategoryForm,
            "library.category",
            gettext_lazy("Device categories"),
            gettext_lazy("New device category"),
        ),
        Kind(
            "room-types",
            RoomType,
            RoomTypeForm,
            "locations.roomtype",
            gettext_lazy("Room types"),
            gettext_lazy("New room type"),
        ),
    )
}


def _actor(request: HttpRequest) -> User:
    assert isinstance(request.user, User)  # noqa: S101  # nosec B101: views require login
    return request.user


def _kind(slug: str) -> Kind:
    if slug not in KINDS:
        raise Http404
    return KINDS[slug]


def save(request: HttpRequest, actor: User, kind: Kind, form: Any, *, created: bool) -> Any:
    """Store a list entry (instance wide, so the permission must be global)."""
    require(actor, f"{kind.permission}.{'add' if created else 'change'}")
    try:
        with transaction.atomic():
            item = form.save()
    except (IntegrityError, ValidationError) as exc:
        raise ServiceError(_("An entry with this key already exists.")) from exc
    audit.record(
        f"{kind.permission}.{'created' if created else 'changed'}",
        request=request,
        data={"key": item.key, "name": item.name},
    )
    return item


def delete(request: HttpRequest, actor: User, kind: Kind, item: Any) -> None:
    """Delete an entry nothing uses."""
    require(actor, f"{kind.permission}.delete")
    if item.key == "other":
        raise ServiceError(_("This entry is the default and cannot be deleted."))
    try:
        with transaction.atomic():
            item.delete()
    except ProtectedError as exc:
        raise ServiceError(_("This entry is in use and cannot be deleted.")) from exc
    audit.record(f"{kind.permission}.deleted", request=request, data={"key": item.key})


@login_required
def index(request: HttpRequest) -> HttpResponse:
    """Both lists with their entries."""
    actor = _actor(request)
    sections = [
        {
            "kind": kind,
            "items": kind.model.objects.all(),
            "can_add": policy.has_perm(actor, f"{kind.permission}.add"),
        }
        for kind in KINDS.values()
        if policy.has_perm(actor, f"{kind.permission}.view")
    ]
    if not sections:
        raise PermissionDenied
    return render(request, "manage/lists.html", {"sections": sections})


@login_required
@require_http_methods(["GET", "POST"])
def create(request: HttpRequest, slug: str) -> HttpResponse:
    """Page that adds an entry to a list."""
    kind = _kind(slug)
    if not policy.has_perm(request.user, f"{kind.permission}.add"):
        raise PermissionDenied
    form = kind.form(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            item = save(request, _actor(request), kind, form, created=True)
        except ServiceError as exc:
            form.add_error(None, str(exc))
        else:
            return redirect("manage:list-item", slug, item.pk)
    return render(
        request,
        "core/create.html",
        {"title": kind.new, "form": form, "back_url": reverse("manage:lists")},
    )


@login_required
@require_http_methods(["GET", "POST"])
def detail(request: HttpRequest, slug: str, pk: int) -> HttpResponse:
    """Edit or delete an entry; room types also list their needs."""
    kind = _kind(slug)
    actor = _actor(request)
    if not policy.has_perm(actor, f"{kind.permission}.view"):
        raise PermissionDenied
    item = get_object_or_404(kind.model, pk=pk)
    form = kind.form(request.POST or None, instance=item)
    if "key" in form.fields and item.builtin:
        form.fields["key"].disabled = True  # built in keys are used by the code and imports
    if request.method == "POST":
        try:
            if request.POST.get("action") == "delete":
                delete(request, actor, kind, item)
                messages.success(request, _("Deleted."))
                return redirect("manage:lists")
            if form.is_valid():
                save(request, actor, kind, form, created=False)
                messages.success(request, _("Saved."))
                return redirect("manage:list-item", slug, pk)
        except ServiceError as exc:
            messages.error(request, str(exc))
            return redirect("manage:list-item", slug, pk)
    return render(
        request,
        "manage/list_item.html",
        {
            "kind": kind,
            "item": item,
            "form": form,
            "needs": item.needs.select_related("category") if kind.model is RoomType else None,
            "can_change": policy.has_perm(actor, f"{kind.permission}.change"),
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def need_create(request: HttpRequest, pk: int) -> HttpResponse:
    """Page that adds a category to what rooms of a type need."""
    room_type = get_object_or_404(RoomType, pk=pk)
    if not policy.has_perm(request.user, "locations.roomtype.change"):
        raise PermissionDenied
    form = NeedForm(request.POST or None, instance=RoomTypeNeed(room_type=room_type))
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                form.save()
        except IntegrityError:
            form.add_error("category", _("This category is already planned for this room type."))
        else:
            audit.record(
                "locations.roomtype.changed",
                request=request,
                data={"key": room_type.key, "need": form.instance.category_id},
            )
            return redirect("manage:list-item", "room-types", pk)
    return render(
        request,
        "core/create.html",
        {
            "title": _("Planned devices for %(type)s") % {"type": room_type},
            "form": form,
            "submit": _("Add"),
            "back_url": reverse("manage:list-item", args=["room-types", pk]),
        },
    )


@login_required
@require_POST
def need_delete(request: HttpRequest, pk: int, need: int) -> HttpResponse:
    """Remove a category from a room type's needs."""
    room_type = get_object_or_404(RoomType, pk=pk)
    require(_actor(request), "locations.roomtype.change")
    RoomTypeNeed.objects.filter(room_type=room_type, pk=need).delete()
    audit.record("locations.roomtype.changed", request=request, data={"key": room_type.key})
    return redirect("manage:list-item", "room-types", pk)
