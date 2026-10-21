"""Tenant scoped objects that live in the ``tenant.site.building.floor.room.rack`` tree."""

from typing import Any, ClassVar

from django.contrib.postgres.indexes import GistIndex

from patchmanager.core.fields import LtreeField, path_label
from patchmanager.tenancy.models import TenantScopedModel


class HierarchyNode(TenantScopedModel):
    """Has a parent (or the tenant) and an ltree ``path`` the policy engine scopes on."""

    path = LtreeField(editable=False)

    parent_field: ClassVar[str | None] = None
    policy_object_type: ClassVar[str]
    condition_attrs: ClassVar[frozenset[str]] = frozenset({"created_by", "name"})

    class Meta:
        abstract = True
        # GiST supports the ``<@`` (descendant) operator used by scoped queries.
        indexes: ClassVar[list[Any]] = [
            GistIndex(fields=["path"], name="%(app_label)s_%(class)s_path")
        ]

    @property
    def parent(self) -> Any:
        """The containing node, or ``None`` for a site."""
        return getattr(self, self.parent_field) if self.parent_field else None

    @property
    def policy_path(self) -> str:
        """Scope path used by the policy engine."""
        return str(self.path)

    def compute_path(self) -> str:
        """``<parent path>.<own label>`` (a site hangs below its tenant)."""
        prefix = self.parent.path if self.parent is not None else path_label(self.tenant_id)
        return f"{prefix}.{path_label(self.pk)}"

    def ancestors(self) -> list[HierarchyNode]:
        """Root first, excluding self."""
        chain: list[HierarchyNode] = []
        node = self.parent
        while node is not None:
            chain.insert(0, node)
            node = node.parent
        return chain

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Inherit the tenant from the parent and keep the path in sync."""
        if self.parent is not None:
            self.tenant_id = self.parent.tenant_id
        self.path = self.compute_path()
        super().save(*args, **kwargs)
