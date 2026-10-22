"""Central authorisation service. Every permission decision goes through here.

Evaluation order for ``(actor, permission, object)``, first match wins:

1. Actor missing, inactive, deactivated or locked: deny.
2. Actor holds the Super Admin role at global scope: allow.
3. A matching deny assignment exists: deny.
4. A matching allow assignment exists: allow.
5. Otherwise: deny.

An assignment matches when its role contains the permission, its scope covers
the object's ``policy_path`` (global scope covers everything, a path covers
itself and its descendants) and its conditions hold. Without an object,
conditional allows do not match while conditional denies do, so unknown
attributes never widen access.
"""

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from django.core.cache import cache
from django.db.models import Model, Q, QuerySet
from django.utils import timezone

from patchmanager.accounts import conditions
from patchmanager.accounts.roles import SUPER_ADMIN, SYSTEM_ROLES
from patchmanager.core.fields import path_label

_VERSION_KEY = "policy:version"
_CACHE_SECONDS = 300


@dataclass(frozen=True)
class Grant:
    """Compiled assignment, as cached per user."""

    assignment_id: UUID
    role_key: str
    role_name: str
    permissions: frozenset[str]
    effect: str
    scope_path: str | None
    scope_label: str
    conditions: tuple[dict[str, Any], ...]
    via_group: str | None
    expires_at: datetime | None

    @property
    def is_super_admin(self) -> bool:
        """Unconditional global Super Admin allow."""
        return (
            self.role_key == SUPER_ADMIN
            and self.effect == "allow"
            and self.scope_path is None
            and not self.conditions
        )

    def grants(self, permission: str) -> bool:
        """True if the role contains ``permission``."""
        return self.is_super_admin or permission in self.permissions

    def covers(self, path: str | None) -> bool:
        """True if the scope covers an object at ``path``."""
        if self.scope_path is None:
            return True
        return path is not None and (
            path == self.scope_path or path.startswith(self.scope_path + ".")
        )

    def active(self, now: datetime) -> bool:
        """Not expired."""
        return self.expires_at is None or self.expires_at > now


@dataclass(frozen=True)
class Decision:
    """Outcome of a check, with the grant that decided it."""

    allowed: bool
    reason: str
    grant: Grant | None = None
    considered: list[Grant] = field(default_factory=list)


def bump_version() -> None:
    """Invalidate every cached grant set. Call after any RBAC change."""
    try:
        cache.incr(_VERSION_KEY)
    except ValueError:
        cache.set(_VERSION_KEY, 2, timeout=None)


def _version() -> int:
    return int(cache.get_or_set(_VERSION_KEY, 1, timeout=None) or 1)


def _group_names(user: Any) -> dict[UUID, str]:
    """The user's groups plus all ancestor groups, by id."""
    from patchmanager.accounts.models import Group

    direct = list(Group.objects.filter(members=user).values_list("id", "name"))
    names = dict(direct)
    edges: dict[UUID, list[UUID]] = {}
    for child, parent in Group.parents.through.objects.values_list("from_group_id", "to_group_id"):
        edges.setdefault(child, []).append(parent)
    queue = deque(names)
    missing: set[UUID] = set()
    while queue:
        for parent in edges.get(queue.popleft(), []):
            if parent not in names and parent not in missing:
                missing.add(parent)
                queue.append(parent)
    if missing:
        names.update(Group.objects.filter(id__in=missing).values_list("id", "name"))
    return names


def mfa_tenant_ids() -> set[UUID]:
    """Tenants that require MFA, read with full visibility (the caller's context may be empty)."""
    from patchmanager.tenancy import rls
    from patchmanager.tenancy.models import Tenant

    with rls.system_context():
        return set(Tenant.objects.filter(requires_mfa=True).values_list("pk", flat=True))


def _compile(user: Any, *, unfiltered: bool = False) -> list[Grant]:
    """Grants of ``user``. API clients lose MFA roles and MFA tenants unless ``unfiltered``."""
    from patchmanager.accounts.models import Assignment

    groups = _group_names(user)
    grants = []
    service = (
        getattr(user, "is_service", False) and not unfiltered
    )  # API clients have no second factor
    mfa_tenants = {path_label(pk) for pk in mfa_tenant_ids()} if service else set()
    for a in Assignment.objects.filter(Q(user=user) | Q(group_id__in=groups)).select_related(
        "role"
    ):
        system = SYSTEM_ROLES.get(a.role.key) if a.role.is_system else None
        needs_mfa = a.role.requires_mfa or bool(system and system.requires_mfa)
        if service and a.effect == "allow" and needs_mfa:
            continue  # MFA roles never apply to API clients, via groups neither
        if (
            a.effect == "allow"
            and a.scope_path
            and str(a.scope_path).split(".", 1)[0] in mfa_tenants
        ):
            continue  # nor grants inside tenants that require MFA
        grants.append(
            Grant(
                assignment_id=a.id,
                role_key=a.role.key,
                role_name=a.role.name,
                permissions=system.permissions() if system else frozenset(a.role.permissions),
                effect=a.effect,
                scope_path=a.scope_path,
                scope_label=a.scope_label,
                conditions=tuple(a.conditions),
                via_group=groups.get(a.group_id) if a.group_id else None,
                expires_at=a.expires_at,
            )
        )
    return grants


def grants_for(user: Any) -> list[Grant]:
    """Active grants of ``user``, cached until the next RBAC change."""
    memo = getattr(user, "_pm_grants", None)
    version = _version()
    if memo is None or memo[0] != version:
        key = f"policy:{version}:{user.pk}"
        grants = cache.get(key)
        if grants is None:
            grants = _compile(user)
            cache.set(key, grants, _CACHE_SECONDS)
        memo = (version, grants)
        user._pm_grants = memo
    now = timezone.now()
    return [g for g in memo[1] if g.active(now)]


def _usable(user: Any) -> bool:
    return bool(
        user is not None
        and getattr(user, "is_authenticated", False)
        and user.is_active
        and user.deactivated_at is None
        and not user.is_locked
    )


def is_super_admin(user: Any) -> bool:
    """True for an active user with the global Super Admin role."""
    return _usable(user) and any(g.is_super_admin for g in grants_for(user))


@dataclass(frozen=True)
class TokenLimits:
    """What an API token may do at most, on top of its user's own rights.

    ``object_types`` (empty: all) and ``scope_path`` (empty: everywhere) narrow every
    check of the request; ``write`` is false for read only tokens. Super admins are
    narrowed too: a token never does more than it was made for.
    """

    object_types: frozenset[str]
    scope_path: str
    write: bool

    def allows(self, permission: str, path: str | None) -> bool:
        """Whether ``permission`` at ``path`` stays inside these limits."""
        object_type, _sep, action = permission.rpartition(".")
        if self.object_types and object_type not in self.object_types:
            return False
        if not self.write and action != "view":
            return False
        if self.scope_path:
            if path is None:  # global objects (the library) are only read
                return action == "view"
            return path == self.scope_path or path.startswith(self.scope_path + ".")
        return True


def token_limits(user: Any) -> TokenLimits | None:
    """The limits of the API token this request uses (None for sessions)."""
    return getattr(user, "api_token_limits", None)


def explain(
    user: Any, permission: str, obj: Any = None, *, scope_path: str | None = None
) -> Decision:
    """Decide ``permission`` for ``user`` on ``obj`` (or at ``scope_path``) and say why."""
    if not _usable(user):
        return Decision(False, "actor inactive, locked or anonymous")
    limits = token_limits(user)
    if limits is not None:
        path = obj.policy_path if obj is not None else scope_path
        if not limits.allows(permission, path):
            return Decision(False, "outside the limits of the API token")
    grants = grants_for(user)
    if any(g.is_super_admin for g in grants):
        return Decision(True, "super admin", next(g for g in grants if g.is_super_admin), grants)
    path = obj.policy_path if obj is not None else scope_path
    relevant = [g for g in grants if g.grants(permission) and g.covers(path)]

    def holds(g: Grant, *, deny: bool) -> bool:
        if not g.conditions:
            return True
        if obj is None:
            return deny
        return conditions.matches(list(g.conditions), obj, user)

    for g in relevant:
        if g.effect == "deny" and holds(g, deny=True):
            return Decision(False, "explicit deny", g, relevant)
    for g in relevant:
        if g.effect == "allow" and holds(g, deny=False):
            return Decision(True, "allowed by assignment", g, relevant)
    return Decision(False, "no matching assignment", None, relevant)


def has_perm(user: Any, permission: str, obj: Any = None, *, scope_path: str | None = None) -> bool:
    """Boolean form of :func:`explain`."""
    return explain(user, permission, obj, scope_path=scope_path).allowed


def has_perm_within(user: Any, permission: str, path: str) -> bool:
    """True if an allow grant gives ``permission`` somewhere at, above or below ``path``.

    An entry gate only (for example "may import racks in this tenant"); every
    object is still checked on its own.
    """
    if not _usable(user):
        return False
    limits = token_limits(user)
    if limits is not None and not (
        limits.allows(permission, path)
        or (
            limits.scope_path.startswith(path + ".")
            and limits.allows(permission, limits.scope_path)
        )
    ):
        return False
    return any(
        g.effect == "allow"
        and g.grants(permission)
        and (g.scope_path is None or g.covers(path) or g.scope_path.startswith(path + "."))
        for g in grants_for(user)
    )


def scope_queryset[M: Model](
    user: Any,
    permission: str,
    queryset: QuerySet[M],
    *,
    path_field: str | None = "path",
    strict_conditions: bool = False,
) -> QuerySet[M]:
    """Restrict ``queryset`` to rows ``user`` may access with ``permission``.

    ``path_field`` names the ltree column holding each row's ``policy_path``;
    pass ``None`` for global objects that only global grants cover. With
    ``strict_conditions`` (rows that are not the object itself, such as history),
    conditional allows are ignored and conditional denies cover their whole scope.
    """
    if not _usable(user):
        return queryset.none()
    limits = token_limits(user)
    if limits is not None:
        if not limits.allows(permission, limits.scope_path or None):
            return queryset.none()
        if limits.scope_path:
            if path_field is None:
                if not permission.endswith(".view"):
                    return queryset.none()
            else:
                queryset = queryset.filter(**{f"{path_field}__descendant_of": limits.scope_path})
    grants = grants_for(user)
    if any(g.is_super_admin for g in grants):
        return queryset
    relevant = [g for g in grants if g.grants(permission)]

    def scope_q(g: Grant) -> Q | None:
        """``None`` means: everything."""
        if g.scope_path is None:
            return None
        if path_field is None:
            return Q(pk__in=[])
        return Q(**{f"{path_field}__descendant_of": g.scope_path})

    if strict_conditions:
        relevant = [g for g in relevant if g.effect == "deny" or not g.conditions]

    def grant_q(g: Grant) -> Q | None:
        use_conditions = g.conditions and not strict_conditions
        parts = [
            q
            for q in (
                scope_q(g),
                conditions.to_q(list(g.conditions), user) if use_conditions else None,
            )
            if q
        ]
        if not parts:
            return None
        q = parts[0]
        for part in parts[1:]:
            q &= part
        return q

    allows = [grant_q(g) for g in relevant if g.effect == "allow"]
    if not allows:
        return queryset.none()
    if None not in allows:
        allow_q = allows[0]
        for q in allows[1:]:
            allow_q |= q  # type: ignore[operator]
        queryset = queryset.filter(allow_q)
    denies = [grant_q(g) for g in relevant if g.effect == "deny"]
    if None in denies:
        return queryset.none()
    for q in denies:
        queryset = queryset.exclude(q)
    return queryset


def accessible_tenants(user: Any) -> tuple[bool, set[UUID]]:
    """Tenants whose rows the database may show: ``(all_tenants, tenant_ids)``.

    This feeds Row Level Security, the coarse outer layer. Fine grained checks
    still run through :func:`has_perm` and :func:`scope_queryset`.
    """
    if not _usable(user):
        return False, set()
    grants = [g for g in grants_for(user) if g.effect == "allow"]
    if getattr(user, "is_service", False):
        # Global grants of API clients must not reach tenants that require MFA either.
        from patchmanager.tenancy import rls
        from patchmanager.tenancy.models import Tenant

        with rls.system_context():
            every = set(Tenant.objects.values_list("pk", flat=True))
        wanted = (
            every
            if any(g.scope_path is None for g in grants)
            else {UUID(hex=g.scope_path.split(".", 1)[0]) for g in grants if g.scope_path}
        )
        return False, wanted - mfa_tenant_ids()
    if any(g.scope_path is None for g in grants):
        return True, set()
    return False, {UUID(hex=g.scope_path.split(".", 1)[0]) for g in grants if g.scope_path}
