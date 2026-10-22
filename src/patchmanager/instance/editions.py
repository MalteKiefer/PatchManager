"""Editions: presets of feature flags and limits, chosen by the operator.

Self hosted instances default to Enterprise (no limits). Billing is out of scope:
an edition only shapes what the instance offers. ``PATCHMANAGER_EDITION`` pins the
edition (the admin page then shows it read only); super admins can otherwise
choose it and override single limits and features.
"""

from dataclasses import dataclass, field, replace
from typing import Any

from django.conf import settings
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from patchmanager.accounts.services import ServiceError
from patchmanager.instance import state

FEATURES = {
    "api": gettext_lazy("REST API and API tokens"),
    "webhooks": gettext_lazy("Webhooks"),
    "sso": gettext_lazy("Single sign on (OIDC, SAML, LDAP)"),
    "scim": gettext_lazy("SCIM provisioning"),
    "import": gettext_lazy("CSV and XLSX import"),
    "floor_plans": gettext_lazy("Floor plans"),
}
REPORTS = ("site", "patch-list", "bom", "rack", "ports")
LIMITS = {
    "racks": gettext_lazy("Racks"),
    "users": gettext_lazy("Users"),
    "tenants": gettext_lazy("Tenants"),
}


@dataclass(frozen=True)
class Edition:
    """Limits (None: unlimited), cabling reach, PDF reports and features."""

    key: str
    name: Any
    limits: dict[str, int | None]
    cross_rack_cabling: bool
    reports: tuple[str, ...]
    features: frozenset[str] = field(default_factory=frozenset)


EDITIONS = {
    e.key: e
    for e in (
        Edition(
            "free",
            gettext_lazy("Free"),
            {"racks": 3, "users": 1, "tenants": 1},
            False,
            ("bom",),
            frozenset({"floor_plans"}),
        ),
        Edition(
            "smb",
            gettext_lazy("SMB"),
            {"racks": None, "users": None, "tenants": 1},
            True,
            REPORTS,
            frozenset({"api", "webhooks", "sso", "import", "floor_plans"}),
        ),
        Edition(
            "integrator",
            gettext_lazy("Integrator"),
            {"racks": None, "users": None, "tenants": 50},
            True,
            REPORTS,
            frozenset(FEATURES),
        ),
        Edition(
            "enterprise",
            gettext_lazy("Enterprise"),
            {"racks": None, "users": None, "tenants": None},
            True,
            REPORTS,
            frozenset(FEATURES),
        ),
    )
}


def pinned() -> str | None:
    """Edition set by the environment, if any."""
    value = getattr(settings, "EDITION", "") or ""
    return value if value in EDITIONS else None


def current() -> Edition:
    """The effective edition with overrides applied."""
    snapshot = state.get()
    key, overrides = snapshot.edition, snapshot.overrides
    edition = EDITIONS.get(pinned() or key, EDITIONS["enterprise"])
    limits = {
        **edition.limits,
        **{k: v for k, v in overrides.get("limits", {}).items() if k in LIMITS},
    }
    features = (set(edition.features) | set(overrides.get("enable", []))) - set(
        overrides.get("disable", [])
    )
    return replace(edition, limits=limits, features=frozenset(f for f in features if f in FEATURES))


def enabled(feature: str) -> bool:
    """True if this edition offers ``feature``."""
    return feature in current().features


def require(feature: str) -> None:
    """Refuse work the edition does not offer."""
    if not enabled(feature):
        raise ServiceError(
            _("%(feature)s is not part of this edition.") % {"feature": FEATURES[feature]}
        )


def usage() -> dict[str, int]:
    """Current counts against the limits (whole instance)."""
    from patchmanager.accounts.models import User
    from patchmanager.dcim.models import Rack
    from patchmanager.tenancy import rls
    from patchmanager.tenancy.models import Tenant

    with rls.system_context():
        return {
            "racks": Rack.objects.count(),
            "users": User.objects.filter(
                is_service=False, is_active=True, deactivated_at__isnull=True
            ).count(),
            "tenants": Tenant.objects.filter(archived_at__isnull=True).count(),
        }


def check_limit(kind: str, adding: int = 1) -> None:
    """Refuse adding ``adding`` more of ``kind`` beyond the edition's limit."""
    limit = current().limits.get(kind)
    if limit is not None and usage()[kind] + adding > limit:
        raise ServiceError(
            _("This edition allows at most %(limit)s %(kind)s.")
            % {"limit": limit, "kind": str(LIMITS[kind]).lower()}
        )


def default_language() -> str:
    """Language for new accounts."""
    return state.get().default_language
