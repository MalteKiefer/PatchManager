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
        Tracked("ipam.vrf", "name", "tenant", "ipam.vrf"),
        Tracked("ipam.prefix", "text", "self", "ipam.prefix"),
        Tracked("ipam.ipaddress", "host", "self", "ipam.ipaddress"),
        Tracked("networks.switchsettings", "hostname", "device", "dcim.device"),
        Tracked("networks.mstinstance", "instance", "device", "dcim.device"),
        Tracked("networks.staticroute", "prefix", "device", "dcim.device"),
        Tracked("networks.bgpneighbor", "address", "device", "dcim.device"),
        Tracked("networks.portprofile", "name", "device", "dcim.device"),
        Tracked("networks.stack", "name", "site", "dcim.device"),
        Tracked("directory.organization", "name", "tenant", "directory.organization"),
        Tracked("directory.system", "name", "tenant", "directory.system"),
        Tracked("ipam.dhcpscope", "name", "tenant", "ipam.dhcpscope"),
        Tracked("ipam.dnszone", "name", "tenant", "ipam.dnszone"),
        Tracked("ipam.dnsrecord", "name", "tenant", "ipam.dnszone"),
        Tracked("circuits.circuit", "circuit_id", "tenant", "circuits.circuit"),
        Tracked("compute.cluster", "name", "tenant", "compute.cluster"),
        Tracked("compute.virtualmachine", "name", "tenant", "compute.vm"),
        Tracked("compute.storagevolume", "name", "tenant", "compute.storage"),
    )
}
