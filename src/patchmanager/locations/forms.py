"""Forms for the location tree and racks."""

from typing import Any

from django import forms
from django.utils.translation import gettext_lazy as _

from patchmanager.core.utils import label_choices
from patchmanager.dcim.models import Rack
from patchmanager.locations.models import Building, Floor, Room, Site
from patchmanager.tenancy.models import Tenant


class SiteForm(forms.ModelForm):  # type: ignore[type-arg]
    """Site details including the map position."""

    class Meta:
        model = Site
        fields = ["name", "code", "address", "latitude", "longitude", "description"]
        widgets = {
            "address": forms.Textarea(attrs={"rows": 3}),
            "description": forms.Textarea(attrs={"rows": 3}),
        }


class BuildingForm(forms.ModelForm):  # type: ignore[type-arg]
    """Building details."""

    class Meta:
        model = Building
        fields = ["name", "description"]
        widgets = {"description": forms.Textarea(attrs={"rows": 2})}


class FloorForm(forms.ModelForm):  # type: ignore[type-arg]
    """Floor details."""

    class Meta:
        model = Floor
        fields = ["name", "level"]


class RoomForm(forms.ModelForm):  # type: ignore[type-arg]
    """Room details."""

    class Meta:
        model = Room
        fields = ["name", "kind", "description"]
        widgets = {"description": forms.Textarea(attrs={"rows": 2})}

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Room types sorted by name."""
        super().__init__(*args, **kwargs)
        label_choices(self.fields["kind"])


class RackForm(forms.ModelForm):  # type: ignore[type-arg]
    """Rack details."""

    class Meta:
        model = Rack
        fields = [
            "name",
            "u_height",
            "width",
            "depth_mm",
            "descending_units",
            "status",
            "serial",
            "asset_tag",
            "notes",
        ]
        widgets = {"notes": forms.Textarea(attrs={"rows": 2})}


class MoveSiteForm(forms.Form):
    """Target tenant for a site move."""

    tenant = forms.ModelChoiceField(label=_("Target tenant"), queryset=Tenant.objects.none())
    confirm = forms.CharField(label=_("Confirm"), help_text=_("Type the site name to confirm."))

    def __init__(self, *args: Any, tenants: Any = None, **kwargs: Any) -> None:
        """``tenants``: possible targets."""
        super().__init__(*args, **kwargs)
        self.fields["tenant"].queryset = tenants  # type: ignore[attr-defined]
