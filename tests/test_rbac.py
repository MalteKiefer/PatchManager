import json

import pytest
import yaml
from django.test import RequestFactory

from patchmanager.accounts import policy
from patchmanager.accounts.models import Assignment, Group, Role
from patchmanager.accounts.services import PermissionDeniedError, ServiceError, rbac

pytestmark = pytest.mark.django_db


@pytest.fixture
def req(super_admin):
    request = RequestFactory().post("/")
    request.user = super_admin
    return request


def test_create_update_delete_custom_role(req, super_admin) -> None:
    role = rbac.create_role(
        req, super_admin, name="Cabler", permission_codes=["tenancy.tenant.view"]
    )
    assert role.key == "cabler" and not role.is_system
    rbac.update_role(
        req,
        super_admin,
        role,
        name="Cabler",
        description="d",
        permission_codes=["tenancy.tenant.view", "tenancy.tenant.change"],
        requires_mfa=True,
    )
    role.refresh_from_db()
    assert role.permissions == ["tenancy.tenant.change", "tenancy.tenant.view"]
    rbac.delete_role(req, super_admin, role)
    assert not Role.objects.filter(pk=role.pk).exists()


def test_unknown_permission_rejected(req, super_admin) -> None:
    with pytest.raises(ServiceError, match="Unknown permissions"):
        rbac.create_role(req, super_admin, name="Bad", permission_codes=["made.up.thing"])


def test_system_roles_are_immutable(req, super_admin) -> None:
    viewer = Role.objects.get(key="viewer")
    with pytest.raises(ServiceError, match="System roles"):
        rbac.update_role(
            req,
            super_admin,
            viewer,
            name="x",
            description="",
            permission_codes=[],
            requires_mfa=False,
        )
    with pytest.raises(ServiceError):
        rbac.delete_role(req, super_admin, viewer)


def test_clone_system_role(req, super_admin) -> None:
    copy = rbac.clone_role(req, super_admin, Role.objects.get(key="editor"), name="Editor plus")
    assert set(copy.permissions) == rbac.role_permissions(Role.objects.get(key="editor"))
    assert not copy.is_system


def test_role_in_use_cannot_be_deleted(req, super_admin, make_user, grant) -> None:
    role = rbac.create_role(req, super_admin, name="Used", permission_codes=["tenancy.tenant.view"])
    Assignment.objects.create(role=role, user=make_user())
    with pytest.raises(ServiceError, match="assignments"):
        rbac.delete_role(req, super_admin, role)


@pytest.mark.parametrize("fmt", ["json", "yaml"])
def test_export_import_roundtrip(req, super_admin, fmt) -> None:
    rbac.create_role(req, super_admin, name="Exported", permission_codes=["tenancy.tenant.view"])
    payload = rbac.export_roles(super_admin, fmt=fmt)
    data = json.loads(payload) if fmt == "json" else yaml.safe_load(payload)
    data["roles"][0]["permissions"].append("tenancy.tenant.change")
    data["roles"].append({"name": "Brand new", "permissions": ["tenancy.tenant.view"]})
    imported = rbac.import_roles(req, super_admin, yaml.safe_dump(data))
    assert {r.name for r in imported} == {"Exported", "Brand new"}
    assert "tenancy.tenant.change" in Role.objects.get(name="Exported").permissions


@pytest.mark.parametrize(
    "payload",
    [
        "{not yaml",
        "version: 2\nroles: []",
        "version: 1\nroles: [{}]",
        "version: 1\nroles: [{name: x, permissions: nope}]",
    ],
)
def test_import_rejects_bad_files(req, super_admin, payload) -> None:
    with pytest.raises(ServiceError):
        rbac.import_roles(req, super_admin, payload)


def test_group_cycle_rejected(req, super_admin) -> None:
    a = rbac.create_group(req, super_admin, name="A")
    b = rbac.create_group(req, super_admin, name="B")
    c = rbac.create_group(req, super_admin, name="C")
    rbac.add_parent(req, super_admin, a, b)
    rbac.add_parent(req, super_admin, b, c)
    with pytest.raises(ServiceError, match="cycle"):
        rbac.add_parent(req, super_admin, c, a)
    with pytest.raises(ServiceError, match="cycle"):
        rbac.add_parent(req, super_admin, a, a)
    rbac.remove_parent(req, super_admin, a, b)
    rbac.add_parent(req, super_admin, c, a)


def test_group_crud_and_members(req, super_admin, make_user) -> None:
    group = rbac.create_group(req, super_admin, name="Ops")
    with pytest.raises(ServiceError, match="already exists"):
        rbac.create_group(req, super_admin, name="ops")
    rbac.update_group(req, super_admin, group, name="Operations", description="x")
    member = make_user()
    rbac.set_members(req, super_admin, group, [member])
    assert list(group.members.all()) == [member]
    rbac.delete_group(req, super_admin, group)
    assert not Group.objects.exists()


def test_no_privilege_escalation(make_user, make_tenant, grant) -> None:
    tenant = make_tenant()
    owner = make_user()
    grant(owner, "tenant_owner", tenant)
    target = make_user()
    request = RequestFactory().post("/")
    request.user = owner
    rbac.assign(request, owner, role=Role.objects.get(key="editor"), user=target, tenant=tenant)
    with pytest.raises(PermissionDeniedError):  # global scope
        rbac.assign(request, owner, role=Role.objects.get(key="editor"), user=target)
    with pytest.raises(PermissionDeniedError):
        rbac.assign(
            request, owner, role=Role.objects.get(key="super_admin"), user=target, tenant=tenant
        )
    with pytest.raises(PermissionDeniedError):  # owner lacks system.settings
        rbac.assign(
            request, owner, role=Role.objects.get(key="super_admin"), user=target, tenant=tenant
        )


def test_assign_validates_principal_and_conditions(req, super_admin, make_user) -> None:
    role = Role.objects.get(key="viewer")
    with pytest.raises(ServiceError):
        rbac.assign(req, super_admin, role=role)
    with pytest.raises(ServiceError, match="attribute not allowed"):
        rbac.assign(
            req,
            super_admin,
            role=role,
            user=make_user(),
            condition_list=[{"attr": "secret", "op": "eq", "value": 1}],
            object_type="tenancy.tenant",
        )


def test_last_super_admin_cannot_be_removed(req, super_admin) -> None:
    assignment = super_admin.assignments.get()
    with pytest.raises(ServiceError, match="last super admin"):
        rbac.unassign(req, super_admin, assignment)


def test_unassign_revokes_access(req, super_admin, make_user, make_tenant) -> None:
    tenant = make_tenant()
    user = make_user()
    a = rbac.assign(req, super_admin, role=Role.objects.get(key="viewer"), user=user, tenant=tenant)
    assert policy.has_perm(user, "tenancy.tenant.view", tenant)
    rbac.unassign(req, super_admin, a)
    assert not policy.has_perm(user, "tenancy.tenant.view", tenant)
