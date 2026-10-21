"""Built in system roles.

Their permissions are resolved from code at runtime, so permissions
registered by later apps are picked up automatically.
"""

from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Any

from django.utils.translation import gettext_lazy as _

from patchmanager.accounts.permissions import all_permissions

SUPER_ADMIN = "super_admin"


@dataclass(frozen=True)
class SystemRole:
    """Definition of an immutable role."""

    key: str
    name: Any
    include: tuple[str, ...]
    exclude: tuple[str, ...] = ()
    requires_mfa: bool = False
    description: Any = ""

    def permissions(self) -> frozenset[str]:
        """Expand the glob patterns against the permission registry."""
        return frozenset(
            perm
            for perm in all_permissions()
            if any(fnmatchcase(perm, pattern) for pattern in self.include)
            and not any(fnmatchcase(perm, pattern) for pattern in self.exclude)
        )


_INSTANCE = ("system.*",)
_IDENTITY = ("accounts.*", "audit.*")

SYSTEM_ROLES: dict[str, SystemRole] = {
    role.key: role
    for role in (
        SystemRole(
            SUPER_ADMIN,
            _("Super Admin"),
            ("*",),
            requires_mfa=True,
            description=_("Full access to the instance, including system settings."),
        ),
        SystemRole(
            "tenant_owner",
            _("Tenant Owner"),
            ("*",),
            (
                *_INSTANCE,
                "tenancy.tenant.add",
                "tenancy.tenant.delete",
                "accounts.role.*",
                "accounts.group.*",
                "accounts.user.delete",
                "accounts.user.impersonate",
                "audit.event.verify",
            ),
            description=_("Everything inside a tenant, including its members and roles."),
        ),
        SystemRole(
            "admin",
            _("Admin"),
            ("*",),
            (
                *_INSTANCE,
                *_IDENTITY,
                "tenancy.tenant.add",
                "tenancy.tenant.delete",
                "*.manage_permissions",
            ),
            description=_("Manage all documentation objects; no identity or system settings."),
        ),
        SystemRole(
            "editor",
            _("Editor"),
            ("*.view", "*.add", "*.change", "*.delete", "*.export", "*.import", "*.trace"),
            (
                *_INSTANCE,
                *_IDENTITY,
                "tenancy.tenant.add",
                "tenancy.tenant.change",
                "tenancy.tenant.delete",
            ),
            description=_("Create, change and delete documentation objects."),
        ),
        SystemRole(
            "viewer",
            _("Viewer"),
            ("*.view",),
            (*_INSTANCE, *_IDENTITY),
            description=_("Read only access to documentation objects."),
        ),
        SystemRole(
            "auditor",
            _("Auditor"),
            ("*.view", "audit.event.export", "audit.event.verify"),
            ("system.*",),
            description=_("Read only access including the audit log."),
        ),
    )
}
