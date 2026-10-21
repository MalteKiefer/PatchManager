from django.apps import AppConfig


class NetworksConfig(AppConfig):
    name = "patchmanager.networks"
    label = "networks"

    def ready(self) -> None:
        from patchmanager.accounts import permissions

        permissions.register("networks.vlan", "VLAN")
        permissions.register("networks.wlan", "WLAN")
