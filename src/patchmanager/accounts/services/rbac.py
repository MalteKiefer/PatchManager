"""Roles, groups and assignments. Every change is audited and invalidates the policy cache."""

import json
from typing import Any

import yaml
from django.db import IntegrityError, transaction
from django.http import HttpRequest
from django.utils.text import slugify
from django.utils.translation import gettext as _

from patchmanager.accounts import conditions, permissions, policy
from patchmanager.accounts.models import Assignment, Group, Role, User
from patchmanager.accounts.roles import SUPER_ADMIN, SYSTEM_ROLES
from patchmanager.accounts.services import PermissionDeniedError, ServiceError
from patchmanager.accounts.services.users import require
from patchmanager.audit import services as audit
from patchmanager.tenancy.models import Tenant

EXPORT_VERSION = 1


def role_permissions(role: Role) -> frozenset[str]:
    """Effective permission codes of a role."""
    system = SYSTEM_ROLES.get(role.key) if role.is_system else None
    return system.permissions() if system else frozenset(role.permissions)


def _validate_permissions(codes: list[str]) -> list[str]:
    unknown = [code for code in codes if not permissions.is_valid(code)]
    if unknown:
        raise ServiceError(_("Unknown permissions: %(codes)s") % {"codes": ", ".join(unknown)})
    return sorted(set(codes))


def _unique_key(name: str) -> str:
    base = slugify(name)[:50] or "role"
    key, n = base, 1
    while Role.objects.filter(key=key).exists():
        n += 1
        key = f"{base}-{n}"
    return key


def _guard_custom(role: Role) -> None:
    if role.is_system:
        raise ServiceError(_("System roles cannot be changed."))


# --- Roles ----------------------------------------------------------------


def create_role(
    request: HttpRequest,
    actor: User,
    *,
    name: str,
    description: str = "",
    permission_codes: list[str],
    requires_mfa: bool = False,
) -> Role:
    """Create a custom role."""
    require(actor, "accounts.role.add")
    codes = _validate_permissions(permission_codes)
    _check_can_add_permissions(actor, set(codes))
    role = Role.objects.create(
        key=_unique_key(name),
        name=name,
        description=description,
        permissions=codes,
        requires_mfa=requires_mfa,
    )
    policy.bump_version()
    audit.record(
        "accounts.role.created", request=request, obj=role, data={"permissions": role.permissions}
    )
    return role


def update_role(
    request: HttpRequest,
    actor: User,
    role: Role,
    *,
    name: str,
    description: str,
    permission_codes: list[str],
    requires_mfa: bool,
) -> Role:
    """Change a custom role."""
    require(actor, "accounts.role.change", role)
    _guard_custom(role)
    before = {"name": role.name, "permissions": role.permissions, "requires_mfa": role.requires_mfa}
    new_permissions = _validate_permissions(permission_codes)
    _check_can_add_permissions(actor, set(new_permissions) - set(role.permissions))
    role.name, role.description, role.requires_mfa = name, description, requires_mfa
    role.permissions = new_permissions
    role.save()
    policy.bump_version()
    after = {"name": role.name, "permissions": role.permissions, "requires_mfa": role.requires_mfa}
    audit.record(
        "accounts.role.changed", request=request, obj=role, data={"before": before, "after": after}
    )
    return role


def clone_role(request: HttpRequest, actor: User, role: Role, *, name: str) -> Role:
    """Copy any role (including system roles) into a new custom role."""
    return create_role(
        request,
        actor,
        name=name,
        description=role.description,
        permission_codes=sorted(role_permissions(role)),
        requires_mfa=role.requires_mfa,
    )


def delete_role(request: HttpRequest, actor: User, role: Role) -> None:
    """Delete a custom role that is not assigned."""
    require(actor, "accounts.role.delete", role)
    _guard_custom(role)
    if role.assignments.exists():
        raise ServiceError(_("Remove the role's assignments first."))
    audit.record("accounts.role.deleted", request=request, obj=role)
    role.delete()
    policy.bump_version()


def export_roles(actor: User, *, fmt: str = "json") -> str:
    """Serialise custom roles for import elsewhere."""
    require(actor, "accounts.role.export")
    data = {
        "version": EXPORT_VERSION,
        "roles": [
            {
                "name": r.name,
                "description": r.description,
                "requires_mfa": r.requires_mfa,
                "permissions": sorted(r.permissions),
            }
            for r in Role.objects.filter(is_system=False).order_by("name")
        ],
    }
    if fmt == "yaml":
        return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    return json.dumps(data, indent=2)


def import_roles(request: HttpRequest, actor: User, payload: str) -> list[Role]:
    """Create or update custom roles by name from JSON or YAML."""
    require(actor, "accounts.role.import")
    try:
        data = yaml.safe_load(payload)  # YAML is a superset of JSON
    except yaml.YAMLError as exc:
        raise ServiceError(_("The file is not valid JSON or YAML.")) from exc
    if (
        not isinstance(data, dict)
        or data.get("version") != EXPORT_VERSION
        or not isinstance(data.get("roles"), list)
    ):
        raise ServiceError(_("Unsupported role file format."))
    result = []
    with transaction.atomic():
        for item in data["roles"]:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                raise ServiceError(_("Every role needs a name."))
            codes = item.get("permissions") or []
            if not isinstance(codes, list):
                raise ServiceError(_("Permissions must be a list."))
            existing = Role.objects.filter(name=item["name"], is_system=False).first()
            kwargs: dict[str, Any] = {
                "name": item["name"][:150],
                "description": str(item.get("description", ""))[:2000],
                "permission_codes": [str(c) for c in codes],
                "requires_mfa": bool(item.get("requires_mfa", False)),
            }
            if existing:
                result.append(update_role(request, actor, existing, **kwargs))
            else:
                result.append(create_role(request, actor, **kwargs))
    return result


# --- Groups ---------------------------------------------------------------


def create_group(request: HttpRequest, actor: User, *, name: str, description: str = "") -> Group:
    """Create a group."""
    require(actor, "accounts.group.add")
    try:
        with transaction.atomic():
            group = Group.objects.create(name=name, description=description)
    except IntegrityError as exc:
        raise ServiceError(_("A group with this name already exists.")) from exc
    audit.record("accounts.group.created", request=request, obj=group)
    return group


def update_group(
    request: HttpRequest, actor: User, group: Group, *, name: str, description: str
) -> Group:
    """Rename or describe a group."""
    require(actor, "accounts.group.change", group)
    group.name, group.description = name, description
    try:
        with transaction.atomic():
            group.save()
    except IntegrityError as exc:
        raise ServiceError(_("A group with this name already exists.")) from exc
    audit.record("accounts.group.changed", request=request, obj=group)
    return group


def delete_group(request: HttpRequest, actor: User, group: Group) -> None:
    """Delete a group and its assignments."""
    require(actor, "accounts.group.delete", group)
    audit.record("accounts.group.deleted", request=request, obj=group)
    group.delete()
    policy.bump_version()


def set_members(request: HttpRequest, actor: User, group: Group, users: list[User]) -> None:
    """Replace the direct members of a group."""
    require(actor, "accounts.group.change", group)
    _check_can_confer(actor, group)
    before = sorted(str(u) for u in group.members.all())
    group.members.set(users)
    policy.bump_version()
    audit.record(
        "accounts.group.members_changed",
        request=request,
        obj=group,
        data={"before": before, "after": sorted(str(u) for u in users)},
    )


def _ancestors(group_id: Any) -> set[Any]:
    """All groups reachable via ``parents`` from ``group_id`` (inclusive)."""
    edges: dict[Any, list[Any]] = {}
    for child, parent in Group.parents.through.objects.values_list("from_group_id", "to_group_id"):
        edges.setdefault(child, []).append(parent)
    seen, stack = {group_id}, [group_id]
    while stack:
        for parent in edges.get(stack.pop(), []):
            if parent not in seen:
                seen.add(parent)
                stack.append(parent)
    return seen


def _check_can_confer(actor: User, group: Group) -> None:
    """Joining ``group`` confers its and its ancestors' assignments.

    The actor must be able to grant every one of them.
    """
    for assignment in Assignment.objects.filter(
        group_id__in=_ancestors(group.pk), effect="allow"
    ).select_related("role"):
        check_can_grant(actor, assignment.role, assignment.scope_path)


def add_parent(request: HttpRequest, actor: User, child: Group, parent: Group) -> None:
    """Nest ``child`` inside ``parent``; rejects cycles."""
    require(actor, "accounts.group.change", child)
    with transaction.atomic():
        # Serialise nesting changes so two concurrent edits cannot form a cycle.
        list(Group.objects.select_for_update().filter(pk__in=[child.pk, parent.pk]))
        if child.pk in _ancestors(parent.pk):
            raise ServiceError(_("This would create a cycle of nested groups."))
        _check_can_confer(actor, parent)
        child.parents.add(parent)
    policy.bump_version()
    audit.record("accounts.group.nested", request=request, obj=child, data={"parent": str(parent)})


def remove_parent(request: HttpRequest, actor: User, child: Group, parent: Group) -> None:
    """Un-nest ``child`` from ``parent``."""
    require(actor, "accounts.group.change", child)
    child.parents.remove(parent)
    policy.bump_version()
    audit.record(
        "accounts.group.unnested", request=request, obj=child, data={"parent": str(parent)}
    )


# --- Assignments ----------------------------------------------------------


def _check_can_add_permissions(actor: User, added: set[str]) -> None:
    """A role may be assigned anywhere, so new permissions must be held globally."""
    if policy.is_super_admin(actor):
        return
    if any(not policy.has_perm(actor, perm) for perm in added):
        raise PermissionDeniedError(_("You cannot add permissions you do not hold yourself."))


def check_can_grant(actor: User, role: Role, scope_path: str | None) -> None:
    """No privilege escalation: the actor must hold every permission of ``role`` at the scope."""
    if policy.is_super_admin(actor):
        return
    if role.key == SUPER_ADMIN:
        raise PermissionDeniedError(_("Only super admins can grant the Super Admin role."))
    missing = [
        p for p in role_permissions(role) if not policy.has_perm(actor, p, scope_path=scope_path)
    ]
    if missing:
        raise PermissionDeniedError(_("You cannot grant permissions you do not hold yourself."))


def _needs_mfa(role: Role) -> bool:
    from patchmanager.accounts.roles import SYSTEM_ROLES

    system = SYSTEM_ROLES.get(role.key)
    return role.requires_mfa or bool(system and system.requires_mfa)


def assign(
    request: HttpRequest,
    actor: User,
    *,
    role: Role,
    user: User | None = None,
    group: Group | None = None,
    tenant: Tenant | None = None,
    node: Any = None,
    effect: str = Assignment.Effect.ALLOW,
    condition_list: list[dict[str, Any]] | None = None,
    object_type: str | None = None,
) -> Assignment:
    """Grant or deny ``role`` to a user or group.

    Scope: global, a tenant, or a location node and everything below it.
    """
    if (user is None) == (group is None):
        raise ServiceError(_("Choose either a user or a group."))
    if (
        user is not None
        and user.is_service
        and effect == Assignment.Effect.ALLOW
        and _needs_mfa(role)
    ):
        raise ServiceError(_("API clients cannot hold roles that require MFA."))
    if node is not None:
        tenant = node.tenant
    scope = node.policy_path if node is not None else tenant.policy_path if tenant else None
    label = (
        " / ".join(str(n) for n in [tenant, *node.ancestors(), node]) if node is not None else None
    )
    require(actor, "accounts.assignment.add", scope_path=scope)
    check_can_grant(actor, role, scope)
    try:
        clauses = conditions.validate(condition_list or [], object_type) if condition_list else []
    except conditions.InvalidConditionError as exc:
        raise ServiceError(str(exc)) from exc
    assignment = Assignment.objects.create(
        role=role,
        user=user,
        group=group,
        tenant=tenant,
        scope_path=scope,
        scope_label=label or (str(tenant) if tenant else "global"),
        effect=effect,
        conditions=clauses,
        created_by=actor,
    )
    policy.bump_version()
    audit.record(
        "accounts.assignment.created",
        request=request,
        obj=assignment,
        tenant_id=getattr(tenant, "pk", None),
        data={
            "role": role.key,
            "principal": str(user or group),
            "scope": assignment.scope_label,
            "effect": effect,
            "conditions": clauses,
        },
    )
    return assignment


def unassign(request: HttpRequest, actor: User, assignment: Assignment) -> None:
    """Remove an assignment; the actor must be able to grant it."""
    require(actor, "accounts.assignment.delete", assignment)
    check_can_grant(actor, assignment.role, assignment.scope_path)
    if (
        assignment.role.key == SUPER_ADMIN
        and assignment.user_id
        and _is_last_super_admin(assignment)
    ):
        raise ServiceError(_("The last super admin cannot be removed."))
    audit.record(
        "accounts.assignment.deleted",
        request=request,
        obj=assignment,
        tenant_id=assignment.tenant_id,
        data={"role": assignment.role.key, "principal": str(assignment.user or assignment.group)},
    )
    assignment.delete()
    policy.bump_version()


def _is_last_super_admin(assignment: Assignment) -> bool:
    return (
        not Assignment.objects.filter(
            role__key=SUPER_ADMIN,
            scope_path__isnull=True,
            effect="allow",
            user__isnull=False,
            user__is_active=True,
        )
        .exclude(pk=assignment.pk)
        .exists()
    )
