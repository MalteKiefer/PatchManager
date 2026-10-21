"""Tracked tables: model label, label column, how the policy scope is found."""

from dataclasses import dataclass
from typing import Any

from django.apps import apps


@dataclass(frozen=True)
class Tracked:
    """A model whose rows get history."""

    label: str  # model label
    name_column: str
    scope: str  # "self" (own path), "device" (its device's path), "site" (its site or tenant)
    policy_type: str

    @property
    def model(self) -> Any:
        """The Django model class."""
        return apps.get_model(self.label)


TRACKED = {
    t.label: t
    for t in (
        Tracked("locations.site", "name", "self", "locations.site"),
        Tracked("locations.building", "name", "self", "locations.building"),
        Tracked("locations.floor", "name", "self", "locations.floor"),
        Tracked("locations.room", "name", "self", "locations.room"),
        Tracked("dcim.rack", "name", "self", "dcim.rack"),
        Tracked("dcim.device", "name", "self", "dcim.device"),
        Tracked("dcim.port", "name", "device", "dcim.device"),
        Tracked("cabling.cable", "label", "self", "cabling.cable"),
        Tracked("networks.vlan", "name", "site", "networks.vlan"),
        Tracked("networks.wlan", "ssid", "tenant", "networks.wlan"),
    )
}
