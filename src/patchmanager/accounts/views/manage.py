"""Admin area: users, groups, roles, assignments, permission matrix and explainer."""

from typing import Any

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods, require_POST

from patchmanager.accounts import permissions, policy
from patchmanager.accounts.decorators import permission_required
from patchmanager.accounts.forms import (
    AssignmentForm,
    CloneForm,
    ExplainForm,
    GroupForm,
    ImportForm,
    InviteForm,
    MembersForm,
    ParentForm,
    ReasonForm,
    RoleForm,
)
from patchmanager.accounts.models import Assignment, Group, Role, User
from patchmanager.accounts.services import ServiceError, rbac, users
from patchmanager.core.utils import parse_uuid
from patchmanager.tenancy.models import Tenant


def _actor(request: HttpRequest) -> User:
    assert isinstance(request.user, User)  # noqa: S101  # nosec B101: guarded by permission_required
    return request.user


def _tenants(request: HttpRequest) -> Any:
    return Tenant.objects.filter(archived_at__isnull=True)  # RLS limits to the actor's tenants


_uuid = parse_uuid


def visible_users(actor: User) -> Any:
    """Global admins see every user; tenant admins see members of their tenants only."""
    if policy.has_perm(actor, "accounts.user.view"):
        return User.objects.all()
    tenant_ids = [
        t.pk
        for t in Tenant.objects.all()
        if policy.has_perm(actor, "accounts.user.view", scope_path=t.policy_path)
    ]
    return User.objects.filter(assignments__tenant_id__in=tenant_ids).distinct()


def _location_models() -> dict[str, Any]:
    from patchmanager.dcim.models import Rack
    from patchmanager.locations.models import Building, Floor, Room, Site

    return {"site": Site, "building": Building, "floor": Floor, "room": Room, "rack": Rack}


def _locations(request: HttpRequest) -> list[tuple[str, str]]:
    """Location nodes the actor may manage assignments for, as form choices."""
    choices = []
    for key, model in _location_models().items():
        for node in model.objects.select_related("tenant").order_by("path"):
            if policy.has_perm(request.user, "accounts.assignment.add", node):
                trail = " / ".join(str(n) for n in [node.tenant, *node.ancestors(), node])
                choices.append((f"{key}:{node.pk}", trail))
    return sorted(choices, key=lambda c: c[1].lower())


def _location(request: HttpRequest, value: str) -> Any:
    """Resolve a ``kind:uuid`` choice (RLS limits it to visible tenants)."""
    if not value:
        return None
    kind, _sep, raw = value.partition(":")
    model = _location_models().get(kind)
    pk = _uuid(raw)
    node = model.objects.filter(pk=pk).first() if model is not None and pk else None
    if node is None:
        raise Http404
    return node


def _run(request: HttpRequest, action: Any, success: str) -> bool:
    """Call a service; turn ``ServiceError`` into a message."""
    try:
        action()
    except ServiceError as exc:
        messages.error(request, str(exc))
        return False
    messages.success(request, success)
    return True


@permission_required("accounts.user.view", any_scope=True)
def dashboard(request: HttpRequest) -> HttpResponse:
    """Entry page of the admin area."""
    return render(
        request,
        "manage/dashboard.html",
        {
            "user_count": User.objects.count(),
            "group_count": Group.objects.count(),
            "role_count": Role.objects.count(),
            "tenant_count": _tenants(request).count(),
        },
    )


# --- Users ----------------------------------------------------------------


@permission_required("accounts.user.view", any_scope=True)
def user_list(request: HttpRequest) -> HttpResponse:
    """Users, searchable."""
    query = request.GET.get("q", "").strip()
    qs = (
        visible_users(_actor(request))
        .annotate(assignment_count=Count("assignments"))
        .order_by("email")
    )
    if query:
        qs = qs.filter(Q(email__icontains=query) | Q(display_name__icontains=query))
    page = Paginator(qs, 50).get_page(request.GET.get("page"))
    return render(request, "manage/users.html", {"page": page, "q": query})


@permission_required("accounts.user.add", any_scope=True)
@require_http_methods(["GET", "POST"])
def user_invite(request: HttpRequest) -> HttpResponse:
    """Invite a new user with an initial role."""
    form = InviteForm(request.POST or None, tenants=_tenants(request))
    if request.method == "POST" and form.is_valid():
        try:
            user, _link = users.invite(request, _actor(request), **form.cleaned_data)
        except ServiceError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, _("Invitation sent to %(email)s.") % {"email": user.email})
            return redirect("manage:user", user.pk)
    return render(request, "manage/user_invite.html", {"form": form})


@permission_required("accounts.user.view", any_scope=True)
def user_detail(request: HttpRequest, pk: str) -> HttpResponse:
    """A user's assignments, groups, factors and sessions."""
    subject = get_object_or_404(visible_users(_actor(request)), pk=pk)
    return render(
        request,
        "manage/user_detail.html",
        {
            "subject": subject,
            "assignments": subject.assignments.select_related("role", "tenant"),
            "groups": subject.pm_groups.all(),
            "factor_count": subject.totp_devices.filter(confirmed=True).count()
            + subject.webauthn_credentials.count(),
            "session_count": subject.sessions.count(),
            "assignment_form": AssignmentForm(
                tenants=_tenants(request), locations=_locations(request)
            ),
            "reason_form": ReasonForm(),
            "is_super_admin": policy.is_super_admin(subject),
            "can_manage_sso": policy.has_perm(request.user, "system.sso.manage"),
        },
    )


@permission_required("accounts.user.view", any_scope=True)
@require_POST
def user_action(request: HttpRequest, pk: str, action: str) -> HttpResponse:
    """Lock, unlock, deactivate, activate, reset MFA, revoke sessions, impersonate."""
    actor = _actor(request)
    subject = get_object_or_404(visible_users(actor), pk=pk)
    actions = {
        "lock": (
            lambda: users.set_locked(request, actor, subject, locked=True),
            _("Account locked."),
        ),
        "unlock": (
            lambda: users.set_locked(request, actor, subject, locked=False),
            _("Account unlocked."),
        ),
        "deactivate": (
            lambda: users.set_active(request, actor, subject, active=False),
            _("Account deactivated."),
        ),
        "activate": (
            lambda: users.set_active(request, actor, subject, active=True),
            _("Account reactivated."),
        ),
        "reset-mfa": (
            lambda: users.reset_mfa(request, actor, subject),
            _("Second factors removed."),
        ),
        "revoke-sessions": (
            lambda: users.revoke_sessions(request, actor, subject),
            _("Sessions signed out."),
        ),
        "break-glass-on": (
            lambda: users.set_break_glass(request, actor, subject, on=True),
            _("This account may now sign in with its password where SSO is enforced."),
        ),
        "break-glass-off": (
            lambda: users.set_break_glass(request, actor, subject, on=False),
            _("This account now follows the SSO rules of its domain."),
        ),
    }
    if action == "impersonate":
        form = ReasonForm(request.POST)
        reason = form.cleaned_data["reason"] if form.is_valid() else ""
        if _run(
            request,
            lambda: users.start_impersonation(request, actor, subject, reason),
            _("You are now acting as %(user)s.") % {"user": subject},
        ):
            return redirect("core:home")
        return redirect("manage:user", pk)
    if action not in actions:
        raise PermissionDenied
    call, message = actions[action]
    _run(request, call, message)
    return redirect("manage:user", pk)


@permission_required("accounts.assignment.view", any_scope=True)
@require_POST
def assignment_create(request: HttpRequest) -> HttpResponse:
    """Grant a role to a user or group."""
    form = AssignmentForm(request.POST, tenants=_tenants(request), locations=_locations(request))
    user_id, group_id = _uuid(request.POST.get("user")), _uuid(request.POST.get("group"))
    user = visible_users(_actor(request)).filter(pk=user_id).first() if user_id else None
    group = Group.objects.filter(pk=group_id).first() if group_id else None
    back = (
        redirect("manage:user", user.pk)
        if user
        else redirect("manage:group", group.pk)
        if group
        else redirect("manage:users")
    )
    if not form.is_valid():
        messages.error(
            request, _("Check the assignment: %(errors)s") % {"errors": form.errors.as_text()}
        )
        return back
    data = form.cleaned_data
    _run(
        request,
        lambda: rbac.assign(
            request,
            _actor(request),
            role=data["role"],
            user=user,
            group=group,
            tenant=data["tenant"],
            node=_location(request, data["location"]),
            effect=data["effect"],
            condition_list=data["conditions"] or None,
            object_type=data["object_type"] or None,
        ),
        _("Assignment added."),
    )
    return back


@permission_required("accounts.assignment.view", any_scope=True)
@require_POST
def assignment_delete(request: HttpRequest, pk: str) -> HttpResponse:
    """Remove an assignment."""
    assignment = get_object_or_404(Assignment, pk=pk)
    back = (
        redirect("manage:user", assignment.user_id)
        if assignment.user_id
        else redirect("manage:group", assignment.group_id)
    )
    _run(
        request,
        lambda: rbac.unassign(request, _actor(request), assignment),
        _("Assignment removed."),
    )
    return back


# --- Groups ---------------------------------------------------------------


@permission_required("accounts.group.view")
def group_list(request: HttpRequest) -> HttpResponse:
    """All groups."""
    groups = Group.objects.annotate(member_count=Count("members", distinct=True)).prefetch_related(
        "parents"
    )
    return render(request, "manage/groups.html", {"groups": groups, "form": GroupForm()})


@permission_required("accounts.group.add")
@require_POST
def group_create(request: HttpRequest) -> HttpResponse:
    """Create a group."""
    form = GroupForm(request.POST)
    if form.is_valid():
        try:
            group = rbac.create_group(request, _actor(request), **form.cleaned_data)
        except ServiceError as exc:
            messages.error(request, str(exc))
        else:
            return redirect("manage:group", group.pk)
    else:
        messages.error(request, form.errors.as_text())
    return redirect("manage:groups")


@permission_required("accounts.group.view")
@require_http_methods(["GET", "POST"])
def group_detail(request: HttpRequest, pk: str) -> HttpResponse:
    """Edit a group, its members, parents and assignments."""
    group = get_object_or_404(Group, pk=pk)
    actor = _actor(request)
    action = request.POST.get("action")
    form = GroupForm(request.POST if action == "details" else None, instance=group)
    members = MembersForm(
        request.POST if action == "members" else None,
        initial={"members": group.members.all()},
    )
    parent = ParentForm(request.POST if action == "parent" else None)
    if request.method == "POST":
        if action == "details" and form.is_valid():
            _run(
                request,
                lambda: rbac.update_group(
                    request,
                    actor,
                    group,
                    name=form.cleaned_data["name"],
                    description=form.cleaned_data["description"],
                ),
                _("Group saved."),
            )
        elif action == "members" and members.is_valid():
            _run(
                request,
                lambda: rbac.set_members(
                    request, actor, group, list(members.cleaned_data["members"])
                ),
                _("Members saved."),
            )
        elif action == "parent" and parent.is_valid():
            _run(
                request,
                lambda: rbac.add_parent(request, actor, group, parent.cleaned_data["parent"]),
                _("Group nested."),
            )
        elif action == "remove_parent":
            parent_id = _uuid(request.POST.get("group"))
            other = get_object_or_404(Group, pk=parent_id) if parent_id else None
            if other is None:
                raise Http404
            _run(
                request,
                lambda: rbac.remove_parent(request, actor, group, other),
                _("Nesting removed."),
            )
        elif action == "delete" and _run(
            request, lambda: rbac.delete_group(request, actor, group), _("Group deleted.")
        ):
            return redirect("manage:groups")
        return redirect("manage:group", pk)
    return render(
        request,
        "manage/group_detail.html",
        {
            "group": group,
            "form": form,
            "members": members,
            "parent_form": parent,
            "assignments": group.assignments.select_related("role", "tenant"),
            "assignment_form": AssignmentForm(
                tenants=_tenants(request), locations=_locations(request)
            ),
        },
    )


# --- Roles and permissions -----------------------------------------------


def _matrix(selected: frozenset[str]) -> list[dict[str, Any]]:
    """Rows of object types with one cell per action."""
    actions = sorted(
        {a for ot in permissions.object_types() for a in ot.actions},
        key=lambda a: (*permissions.STANDARD_ACTIONS, a).index(a),
    )
    return [
        {
            "type": ot,
            "cells": [
                {
                    "action": a,
                    "code": f"{ot.code}.{a}",
                    "exists": a in ot.actions,
                    "on": f"{ot.code}.{a}" in selected,
                }
                for a in actions
            ],
        }
        for ot in permissions.object_types()
    ]


@permission_required("accounts.role.view")
def role_list(request: HttpRequest) -> HttpResponse:
    """System and custom roles."""
    roles = Role.objects.annotate(assignment_count=Count("assignments"))
    return render(request, "manage/roles.html", {"roles": roles, "import_form": ImportForm()})


@permission_required("accounts.role.add")
@require_http_methods(["GET", "POST"])
def role_create(request: HttpRequest) -> HttpResponse:
    """New custom role with permission matrix."""
    form = RoleForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            role = rbac.create_role(
                request,
                _actor(request),
                name=form.cleaned_data["name"],
                description=form.cleaned_data["description"],
                permission_codes=request.POST.getlist("permissions"),
                requires_mfa=form.cleaned_data["requires_mfa"],
            )
        except ServiceError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, _("Role created."))
            return redirect("manage:role", role.pk)
    selected = frozenset(request.POST.getlist("permissions"))
    return render(
        request,
        "manage/role_detail.html",
        {"form": form, "matrix": _matrix(selected), "role": None},
    )


@permission_required("accounts.role.view")
@require_http_methods(["GET", "POST"])
def role_detail(request: HttpRequest, pk: str) -> HttpResponse:
    """View or edit a role."""
    role = get_object_or_404(Role, pk=pk)
    form = RoleForm(request.POST or None, instance=role)
    if request.method == "POST" and form.is_valid():
        try:
            rbac.update_role(
                request,
                _actor(request),
                role,
                name=form.cleaned_data["name"],
                description=form.cleaned_data["description"],
                permission_codes=request.POST.getlist("permissions"),
                requires_mfa=form.cleaned_data["requires_mfa"],
            )
        except ServiceError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, _("Role saved."))
            return redirect("manage:role", role.pk)
    return render(
        request,
        "manage/role_detail.html",
        {
            "form": form,
            "role": role,
            "matrix": _matrix(rbac.role_permissions(role)),
            "clone_form": CloneForm(),
        },
    )


@permission_required("accounts.role.add")
@require_POST
def role_clone(request: HttpRequest, pk: str) -> HttpResponse:
    """Copy a role."""
    role = get_object_or_404(Role, pk=pk)
    form = CloneForm(request.POST)
    if not form.is_valid():
        messages.error(request, form.errors.as_text())
        return redirect("manage:role", pk)
    try:
        copy = rbac.clone_role(request, _actor(request), role, name=form.cleaned_data["name"])
    except ServiceError as exc:
        messages.error(request, str(exc))
        return redirect("manage:role", pk)
    messages.success(request, _("Role copied."))
    return redirect("manage:role", copy.pk)


@permission_required("accounts.role.delete")
@require_POST
def role_delete(request: HttpRequest, pk: str) -> HttpResponse:
    """Delete a custom role."""
    role = get_object_or_404(Role, pk=pk)
    if _run(request, lambda: rbac.delete_role(request, _actor(request), role), _("Role deleted.")):
        return redirect("manage:roles")
    return redirect("manage:role", pk)


@permission_required("accounts.role.export")
def role_export(request: HttpRequest) -> HttpResponse:
    """Download custom roles as JSON or YAML."""
    fmt = "yaml" if request.GET.get("format") == "yaml" else "json"
    body = rbac.export_roles(_actor(request), fmt=fmt)
    response = HttpResponse(
        body, content_type="application/yaml" if fmt == "yaml" else "application/json"
    )
    response["Content-Disposition"] = f'attachment; filename="patchmanager-roles.{fmt}"'
    from patchmanager.audit import services as audit

    audit.record("accounts.role.exported", request=request, data={"format": fmt})
    return response


@permission_required("accounts.role.import")
@require_POST
def role_import(request: HttpRequest) -> HttpResponse:
    """Upload role definitions."""
    form = ImportForm(request.POST, request.FILES)
    if not form.is_valid():
        messages.error(request, form.errors.as_text())
        return redirect("manage:roles")
    try:
        imported = rbac.import_roles(request, _actor(request), form.cleaned_data["file"])
    except ServiceError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, _("%(count)s roles imported.") % {"count": len(imported)})
    return redirect("manage:roles")


@permission_required("accounts.role.view")
def permission_matrix(request: HttpRequest) -> HttpResponse:
    """Roles side by side: which role grants what."""
    roles = list(Role.objects.all())
    granted = {r.pk: rbac.role_permissions(r) for r in roles}
    rows = [
        {"code": p, "cells": [p in granted[r.pk] for r in roles]}
        for p in permissions.all_permissions()
    ]
    return render(request, "manage/permissions.html", {"roles": roles, "rows": rows})


@permission_required("accounts.role.view")
def permission_registry(request: HttpRequest) -> JsonResponse:
    """Machine readable list of object types and actions."""
    return JsonResponse(
        {
            "object_types": [
                {
                    "code": ot.code,
                    "label": ot.label,
                    "actions": list(ot.actions),
                    "condition_attributes": sorted(ot.condition_attrs),
                }
                for ot in permissions.object_types()
            ]
        }
    )


@permission_required("accounts.assignment.view")
def explain_view(request: HttpRequest) -> HttpResponse:
    """Why can or can't user X do Y on Z."""
    form = ExplainForm(request.GET or None)
    decision = None
    if form.is_valid():
        tenant = form.cleaned_data["tenant"]
        decision = policy.explain(
            form.cleaned_data["user"], form.cleaned_data["permission"], tenant
        )
    return render(request, "manage/explain.html", {"form": form, "decision": decision})
