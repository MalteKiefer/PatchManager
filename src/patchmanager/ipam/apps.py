from django.apps import AppConfig


class IpamConfig(AppConfig):
    name = "patchmanager.ipam"
    label = "ipam"

    def ready(self) -> None:
        from patchmanager.accounts import permissions

        permissions.register("ipam.vrf", "VRF")
        permissions.register("ipam.prefix", "Prefix")
        permissions.register("ipam.ipaddress", "IP address")
