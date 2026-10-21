"""Permission registry. Permissions are declared in code, never in the database.

A permission code is ``<app>.<object>.<action>``, for example
``tenancy.tenant.change``. Domain apps register their object types in their
``AppConfig.ready``.
"""

from dataclasses import dataclass, field

STANDARD_ACTIONS = ("view", "add", "change", "delete", "export", "import", "manage_permissions")


@dataclass(frozen=True)
class ObjectType:
    """A permission bearing object type."""

    code: str
    label: str
    actions: tuple[str, ...]
    condition_attrs: frozenset[str] = field(default_factory=frozenset)

    def permissions(self) -> list[str]:
        """All permission codes of this type."""
        return [f"{self.code}.{action}" for action in self.actions]


_REGISTRY: dict[str, ObjectType] = {}


def register(
    code: str,
    label: str,
    *,
    extra_actions: tuple[str, ...] = (),
    actions: tuple[str, ...] = STANDARD_ACTIONS,
    condition_attrs: frozenset[str] = frozenset(),
) -> ObjectType:
    """Declare an object type and its actions."""
    object_type = ObjectType(code, label, (*actions, *extra_actions), condition_attrs)
    _REGISTRY[code] = object_type
    return object_type


def object_types() -> list[ObjectType]:
    """Registered object types, sorted by code."""
    return sorted(_REGISTRY.values(), key=lambda ot: ot.code)


def all_permissions() -> list[str]:
    """Every registered permission code."""
    return [perm for ot in object_types() for perm in ot.permissions()]


def get_object_type(code: str) -> ObjectType:
    """Look up an object type; raises ``KeyError`` for unknown codes."""
    return _REGISTRY[code]


def split(permission: str) -> tuple[str, str]:
    """Split ``app.object.action`` into object type code and action."""
    object_type, _, action = permission.rpartition(".")
    return object_type, action


def is_valid(permission: str) -> bool:
    """True if ``permission`` is registered."""
    object_type, action = split(permission)
    return object_type in _REGISTRY and action in _REGISTRY[object_type].actions
