"""List, create and detail pages for simple tenant records (one place for many types).

A :class:`Resource` names a model, its form and the columns of its list. Every page
checks the model's permission type (``policy_object_type``) on the object, or on the
tenant for lists and new records. Records with a ``parent`` are created from the
parent's page and governed by the parent's permission.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Model, ProtectedError
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods

from patchmanager.accounts import policy
from patchmanager.accounts.models import User
from patchmanager.accounts.services import PermissionDeniedError, ServiceError
from patchmanager.accounts.services.users import require
from patchmanager.audit import services as audit
from patchmanager.core.fields import path_label
from patchmanager.core.utils import parse_uuid


@dataclass(frozen=True)
class Resource:
    """One kind of record with its pages."""

    key: str  # URL part, for example "organizations"
    model: Any
    form: Any  # ModelForm class; its __init__ takes tenant= and actor=
    title: Any  # plural, for the list
    new: Any  # title of the create page
    group: str  # tabs shown together
    columns: tuple[tuple[Any, str], ...]
    parent: tuple[str, str] | None = None  # (field, resource key) for child records
    children: tuple[str, ...] = ()  # resource keys listed on the detail page
    context: Callable[[HttpRequest, Any], dict[str, Any]] | None = None
    template: str = ""  # extra content of the detail page
    hidden: bool = False  # no own list (child records)
    # (label, URL name) of imports, shown next to "New" to those who may also import
    actions: tuple[tuple[Any, str], ...] = ()
    order: tuple[str, ...] = field(default=("name",))


REGISTRY: dict[str, Resource] = {}
GROUPS: dict[str, list[str]] = {}


def register(resource: Resource) -> Resource:
    """Make the pages of ``resource`` available."""
    REGISTRY[resource.key] = resource
    if not resource.hidden:
        GROUPS.setdefault(resource.group, []).append(resource.key)
    return resource


def _resource(key: str) -> Resource:
    if key not in REGISTRY:
        raise Http404
    return REGISTRY[key]


def _actor(request: HttpRequest) -> User:
    assert isinstance(request.user, User)  # noqa: S101  # nosec B101: views require login
    return request.user


def _tenant(request: HttpRequest) -> Any:
    tenant = getattr(request, "tenant", None)
    if tenant is None:
        raise Http404
    return tenant


def _perm(model: Any, action: str) -> str:
    return f"{model.policy_object_type}.{action}"


def cell(obj: Any, attr: str) -> Any:
    """The shown value of ``attr`` (choices as their label, empty as blank)."""
    display = getattr(obj, f"get_{attr}_display", None)
    value = display() if display else getattr(obj, attr, "")
    value = value() if callable(value) else value
    if isinstance(value, list | tuple):
        return ", ".join(map(str, value))
    return "" if value is None else value


# --- Services ------------------------------------------------------------------


def _save(obj: Model, message: str) -> None:
    try:
        obj.full_clean(exclude=["tenant"])
    except ValidationError as exc:
        raise ServiceError("; ".join(exc.messages)) from exc
    try:
        with transaction.atomic():
            obj.save()
    except IntegrityError as exc:
        raise ServiceError(message) from exc


def create(
    request: HttpRequest, actor: User, resource: Resource, tenant: Any, **fields: Any
) -> Any:
    """Create a record in ``tenant`` (or below a parent the user may change)."""
    obj = resource.model(tenant=tenant, created_by=actor, **fields)
    require(actor, _perm(resource.model, "add"), scope_path=obj.policy_path)
    _save(obj, _("This entry already exists."))
    audit.record(
        f"{resource.model.policy_object_type}.created",
        request=request,
        obj=obj,
        data={"name": str(obj)[:200]},
    )
    return obj


def update(request: HttpRequest, actor: User, obj: Any, **fields: Any) -> Any:
    """Change a record (authorised on the stored row and on the result)."""
    stored = type(obj).objects.get(pk=obj.pk)
    require(actor, _perm(stored, "change"), stored)
    for key, value in fields.items():
        setattr(stored, key, value)
    require(actor, _perm(stored, "change"), stored)
    _save(stored, _("This entry already exists."))
    audit.record(f"{stored.policy_object_type}.changed", request=request, obj=stored)
    return stored


def delete(request: HttpRequest, actor: User, obj: Any) -> None:
    """Delete a record with its photos and notes."""
    from patchmanager.metadata import services as metadata

    require(actor, _perm(obj, "delete"), obj)
    try:
        with transaction.atomic():
            metadata.purge(obj)
            obj.delete()
    except ProtectedError as exc:
        raise ServiceError(_("Other records still use this one.")) from exc
    audit.record(
        f"{obj.policy_object_type}.deleted", request=request, obj=obj, data={"name": str(obj)[:200]}
    )


# --- Views ---------------------------------------------------------------------


def _tabs(resource: Resource) -> list[Resource]:
    return [REGISTRY[k] for k in GROUPS.get(resource.group, [])]


@login_required
def index(request: HttpRequest, key: str) -> HttpResponse:
    """The records of one kind in the current tenant that the user may see."""
    resource = _resource(key)
    if resource.hidden:
        raise Http404
    tenant = _tenant(request)
    rows = [
        o
        for o in resource.model.objects.filter(tenant=tenant).order_by(*resource.order)
        if policy.has_perm(request.user, _perm(resource.model, "view"), o)
    ]
    return render(
        request,
        "core/crud_list.html",
        {
            "resource": resource,
            "tabs": _tabs(resource),
            "headers": [label for label, _attr in resource.columns],
            "rows": [(o, [cell(o, attr) for _label, attr in resource.columns]) for o in rows],
            "can_add": policy.has_perm(
                request.user, _perm(resource.model, "add"), scope_path=path_label(tenant.pk)
            ),
            "can_import": policy.has_perm(
                request.user, _perm(resource.model, "import"), scope_path=path_label(tenant.pk)
            ),
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def new(request: HttpRequest, key: str) -> HttpResponse:
    """Page that creates a record (a child record names its parent with ``?parent=``)."""
    resource = _resource(key)
    tenant = _tenant(request)
    actor = _actor(request)
    initial: dict[str, Any] = {}
    back = reverse("objects:index", args=[key]) if not resource.hidden else "/"
    parent = None
    if resource.parent is not None:
        _field, parent_key = resource.parent
        parent_model = REGISTRY[parent_key].model
        parent = parent_model.objects.filter(
            pk=parse_uuid(request.POST.get("parent") or request.GET.get("parent"))
        ).first()
        if parent is None or not policy.has_perm(actor, _perm(parent, "change"), parent):
            raise Http404
        tenant = parent.tenant
        back = reverse("objects:detail", args=[parent_key, parent.pk])
    elif not policy.has_perm(actor, _perm(resource.model, "add"), scope_path=path_label(tenant.pk)):
        raise PermissionDenied
    form = resource.form(request.POST or None, initial=initial, tenant=tenant, actor=actor)
    if resource.parent is not None:
        form.fields.pop(resource.parent[0], None)
    if request.method == "POST" and form.is_valid():
        fields = dict(form.cleaned_data)
        if resource.parent is not None:
            fields[resource.parent[0]] = parent
        try:
            obj = create(request, actor, resource, tenant, **fields)
        except PermissionDeniedError as exc:
            raise PermissionDenied from exc
        except ServiceError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, _("Created."))
            if resource.parent is not None:
                return redirect(back)
            return redirect("objects:detail", key, obj.pk)
    return render(
        request,
        "core/create.html",
        {
            "title": resource.new,
            "form": form,
            "back_url": back,
            "hidden": {"parent": parent.pk} if parent is not None else {},
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def detail(request: HttpRequest, key: str, pk: str) -> HttpResponse:
    """Edit or delete a record; its related records, links, photos and notes."""
    resource = _resource(key)
    obj = get_object_or_404(resource.model, pk=pk)  # RLS hides other tenants
    actor = _actor(request)
    if not policy.has_perm(actor, _perm(obj, "view"), obj):
        raise Http404
    action = request.POST.get("action")
    form = resource.form(
        request.POST if action == "edit" else None, instance=obj, tenant=obj.tenant, actor=actor
    )
    if resource.parent is not None:
        form.fields.pop(resource.parent[0], None)
    parent: Any = getattr(obj, resource.parent[0]) if resource.parent else None
    back = (
        reverse("objects:detail", args=[resource.parent[1], parent.pk])
        if resource.parent
        else reverse("objects:index", args=[key])
    )
    if request.method == "POST":
        try:
            if action == "delete":
                delete(request, actor, obj)
                messages.success(request, _("Deleted."))
                return redirect(back)
            if action == "edit" and form.is_valid():
                update(request, actor, obj, **form.cleaned_data)
                messages.success(request, _("Saved."))
                return redirect("objects:detail", key, obj.pk)
        except PermissionDeniedError as exc:
            raise PermissionDenied from exc
        except ServiceError as exc:
            messages.error(request, str(exc))
            return redirect("objects:detail", key, obj.pk)
    children = []
    for child_key in resource.children:
        child = REGISTRY[child_key]
        assert child.parent is not None  # noqa: S101  # nosec B101: children have a parent
        rows = child.model.objects.filter(**{child.parent[0]: obj}).order_by(*child.order)
        children.append(
            {
                "resource": child,
                "headers": [label for label, _attr in child.columns],
                "rows": [(o, [cell(o, attr) for _l, attr in child.columns]) for o in rows],
            }
        )
    return render(
        request,
        "core/crud_detail.html",
        {
            "resource": resource,
            "obj": obj,
            "form": form,
            "back_url": back,
            "parent": parent,
            "children": children,
            "can_change": policy.has_perm(actor, _perm(obj, "change"), obj),
            "can_delete": policy.has_perm(actor, _perm(obj, "delete"), obj),
            **(resource.context(request, obj) if resource.context else {}),
        },
    )
