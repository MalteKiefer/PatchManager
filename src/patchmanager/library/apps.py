from django.apps import AppConfig


class LibraryConfig(AppConfig):
    name = "patchmanager.library"
    label = "library"

    def ready(self) -> None:
        from patchmanager.accounts import permissions

        permissions.register("library.vendor", "Vendor")
        permissions.register("library.devicetype", "Device type")
