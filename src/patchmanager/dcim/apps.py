from django.apps import AppConfig


class DcimConfig(AppConfig):
    name = "patchmanager.dcim"
    label = "dcim"

    def ready(self) -> None:
        from patchmanager.accounts import permissions
        from patchmanager.dcim.models import Rack

        permissions.register("dcim.rack", "Rack", condition_attrs=Rack.condition_attrs)
