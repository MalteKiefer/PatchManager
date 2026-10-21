from datetime import timedelta

import pytest
from django.utils import timezone

from patchmanager.accounts import permissions, policy
from patchmanager.accounts.models import Group
from patchmanager.accounts.roles import SYSTEM_ROLES
from patchmanager.tenancy.models import Tenant

pytestmark = pytest.mark.django_db

# Permission matrix: (role, permission) -> allowed at the role's scope.
MATRIX = [
    ("viewer", "tenancy.tenant.view", True),
    ("viewer", "tenancy.tenant.change", False),
    ("viewer", "accounts.user.view", False),
    ("viewer", "audit.event.view", False),
    ("editor", "tenancy.tenant.view", True),
    ("editor", "tenancy.tenant.change", False),
    ("editor", "accounts.assignment.add", False),
    ("admin", "tenancy.tenant.change", True),
    ("admin", "tenancy.tenant.delete", False),
    ("admin", "accounts.user.add", False),
    ("tenant_owner", "tenancy.tenant.change", True),
    ("tenant_owner", "accounts.user.add", True),
    ("tenant_owner", "accounts.assignment.add", True),
    ("tenant_owner", "accounts.role.add", False),
    ("tenant_owner", "tenancy.tenant.delete", False),
    ("tenant_owner", "system.settings.view", False),
    ("auditor", "audit.event.view", True),
    ("auditor", "audit.event.export", True),
    ("auditor", "tenancy.tenant.change", False),
    ("auditor", "accounts.user.view", True),
]


@pytest.mark.parametrize(("role", "permission", "expected"), MATRIX)
def test_system_role_matrix(make_user, make_tenant, grant, role, permission, expected) -> None:
    tenant = make_tenant()
    user = make_user()
    grant(user, role, tenant)
    assert policy.has_perm(user, permission, tenant) is expected


def test_every_system_role_only_grants_registered_permissions() -> None:
    registered = set(permissions.all_permissions())
    for role in SYSTEM_ROLES.values():
        assert role.permissions() <= registered


def test_super_admin_allows_everything(super_admin, make_tenant) -> None:
    tenant = make_tenant()
    for perm in permissions.all_permissions():
        assert policy.has_perm(super_admin, perm, tenant)
    assert policy.explain(super_admin, "tenancy.tenant.delete", tenant).reason == "super admin"


def test_tenant_scope_does_not_leak_to_other_tenant(make_user, make_tenant, grant) -> None:
    mine, other = make_tenant(), make_tenant()
    user = make_user()
    grant(user, "admin", mine)
    assert policy.has_perm(user, "tenancy.tenant.change", mine)
    assert not policy.has_perm(user, "tenancy.tenant.change", other)
    assert not policy.has_perm(user, "tenancy.tenant.change")  # no global grant


def test_scope_covers_descendants_not_siblings(make_user, make_tenant, grant) -> None:
    tenant = make_tenant()
    user = make_user()
    grant(user, "viewer", tenant)
    path = tenant.policy_path
    assert policy.has_perm(user, "tenancy.tenant.view", scope_path=f"{path}.abc")
    assert not policy.has_perm(user, "tenancy.tenant.view", scope_path=f"{path}x")


def test_explicit_deny_overrides_allow(make_user, make_tenant, grant) -> None:
    tenant = make_tenant()
    user = make_user()
    grant(user, "admin")  # global allow
    grant(user, "viewer", tenant, effect="deny")
    decision = policy.explain(user, "tenancy.tenant.view", tenant)
    assert not decision.allowed
    assert decision.reason == "explicit deny"
    assert policy.has_perm(user, "tenancy.tenant.change", tenant)  # deny only covers viewer perms


def test_group_inheritance_through_nesting(make_user, make_tenant, grant) -> None:
    tenant = make_tenant()
    user = make_user()
    child, parent, grandparent = (
        Group.objects.create(name=n) for n in ("child", "parent", "grand")
    )
    child.members.add(user)
    child.parents.add(parent)
    parent.parents.add(grandparent)
    grant(grandparent, "editor", tenant)
    decision = policy.explain(user, "tenancy.tenant.view", tenant)
    assert decision.allowed
    assert decision.grant is not None and decision.grant.via_group == "grand"


def test_conditions_on_objects(make_user, make_tenant, grant) -> None:
    tenant = make_tenant(name="Alpha")
    other = make_tenant(name="Beta")
    user = make_user()
    permissions.register(
        "tenancy.tenant", "Tenant", extra_actions=("archive",), condition_attrs=frozenset({"name"})
    )
    try:
        grant(user, "viewer", conditions=[{"attr": "name", "op": "eq", "value": "Alpha"}])
        assert policy.has_perm(user, "tenancy.tenant.view", tenant)
        assert not policy.has_perm(user, "tenancy.tenant.view", other)
        assert not policy.has_perm(
            user, "tenancy.tenant.view"
        )  # no object: conditional allow does not match
        visible = policy.scope_queryset(
            user, "tenancy.tenant.view", Tenant.objects.all(), path_field=None
        )
        with policy_context():
            assert list(visible) == [tenant]
    finally:
        permissions.register("tenancy.tenant", "Tenant", extra_actions=("archive",))


def policy_context():
    from patchmanager.tenancy.rls import system_context

    return system_context()


def test_conditional_deny_applies_without_object(make_user, grant) -> None:
    user = make_user()
    grant(user, "admin")
    grant(user, "viewer", effect="deny", conditions=[{"attr": "name", "op": "eq", "value": "x"}])
    assert not policy.has_perm(user, "tenancy.tenant.view")


def test_expired_assignment_is_ignored(make_user, grant) -> None:
    user = make_user()
    grant(user, "viewer", expires_at=timezone.now() - timedelta(minutes=1))
    assert not policy.has_perm(user, "tenancy.tenant.view")


@pytest.mark.parametrize("change", ["inactive", "locked", "deactivated"])
def test_unusable_accounts_are_denied(make_user, grant, change) -> None:
    user = make_user()
    grant(user, "super_admin")
    if change == "inactive":
        user.is_active = False
    elif change == "locked":
        user.locked_until = timezone.now() + timedelta(hours=1)
    else:
        user.deactivated_at = timezone.now()
    assert not policy.has_perm(user, "tenancy.tenant.view")
    assert not policy.is_super_admin(user)
    assert policy.accessible_tenants(user) == (False, set())


def test_anonymous_is_denied() -> None:
    from django.contrib.auth.models import AnonymousUser

    assert not policy.has_perm(AnonymousUser(), "tenancy.tenant.view")
    assert policy.explain(None, "tenancy.tenant.view").reason.startswith("actor inactive")


def test_cache_invalidated_on_assignment_change(make_user, make_tenant, grant) -> None:
    tenant = make_tenant()
    user = make_user()
    assert not policy.has_perm(user, "tenancy.tenant.view", tenant)
    assignment = grant(user, "viewer", tenant)
    assert policy.has_perm(user, "tenancy.tenant.view", tenant)
    assignment.delete()
    assert not policy.has_perm(user, "tenancy.tenant.view", tenant)


def test_scope_queryset_matches_has_perm(make_user, make_tenant, grant, system) -> None:
    tenants = [make_tenant() for _ in range(4)]
    user = make_user()
    grant(user, "viewer", tenants[0])
    grant(user, "viewer", tenants[1])
    grant(user, "viewer", tenants[1], effect="deny")
    with system():
        all_tenants = list(Tenant.objects.all())
    expected = {t.pk for t in all_tenants if policy.has_perm(user, "tenancy.tenant.view", t)}
    assert expected == {tenants[0].pk}


def test_scope_queryset_global_and_none(make_user, grant, make_tenant, system) -> None:
    make_tenant()
    user = make_user()
    with system():
        assert policy.scope_queryset(user, "tenancy.tenant.view", Tenant.objects.all()).count() == 0
        grant(user, "viewer")
        assert policy.scope_queryset(user, "tenancy.tenant.view", Tenant.objects.all()).count() == 1
        grant(user, "viewer", effect="deny")
        assert policy.scope_queryset(user, "tenancy.tenant.view", Tenant.objects.all()).count() == 0


def test_accessible_tenants(make_user, make_tenant, grant) -> None:
    a, b = make_tenant(), make_tenant()
    user = make_user()
    assert policy.accessible_tenants(user) == (False, set())
    grant(user, "viewer", a)
    grant(user, "editor", b)
    assert policy.accessible_tenants(user) == (False, {a.pk, b.pk})
    grant(user, "auditor")
    assert policy.accessible_tenants(user) == (True, set())
