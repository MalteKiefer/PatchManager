"""Pages of lines (through the shared record pages)."""

from typing import Any

from django import forms
from django.utils.translation import gettext_lazy as _

from patchmanager.circuits.models import Circuit
from patchmanager.core import crud
from patchmanager.dcim.models import Port
from patchmanager.directory.pages import TenantForm


class CircuitForm(TenantForm):
    """A line with its provider and contract."""

    tenant_fields = ("provider", "site")

    class Meta:
        model = Circuit
        fields = [
            "circuit_id",
            "provider",
            "kind",
            "status",
            "site",
            "port",
            "download_mbps",
            "upload_mbps",
            "addresses",
            "contract_number",
            "contract_end",
            "notice_days",
            "monthly_cost",
            "support",
            "description",
        ]
        widgets = {
            "contract_end": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "description": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args: Any, tenant: Any, actor: Any, **kwargs: Any) -> None:
        """Handover ports: interfaces of routers, firewalls and modems of the tenant."""
        super().__init__(*args, tenant=tenant, actor=actor, **kwargs)
        field: Any = self.fields["port"]
        field.queryset = (
            Port.objects.filter(
                tenant=tenant,
                kind="interface",
                device__device_type__category__in=["router", "firewall", "other"],
            )
            .select_related("device")
            .order_by("device__name", "order")
        )
        field.label_from_instance = lambda p: f"{p.device.name} {p.name}"


crud.register(
    crud.Resource(
        key="circuits",
        model=Circuit,
        form=CircuitForm,
        title=_("Lines"),
        new=_("New line"),
        group="directory",
        columns=(
            (_("Line ID"), "circuit_id"),
            (_("Provider"), "provider"),
            (_("Kind"), "kind"),
            (_("Site"), "site"),
            (_("Status"), "status"),
            (_("Contract end"), "contract_end"),
        ),
        order=("provider__name", "circuit_id"),
    )
)
