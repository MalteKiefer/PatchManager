from django.apps import AppConfig
from django.db.models.signals import post_migrate


class LibraryConfig(AppConfig):
    name = "patchmanager.library"
    label = "library"

    def ready(self) -> None:
        from patchmanager.accounts import permissions

        permissions.register("library.vendor", "Vendor")
        permissions.register("library.devicetype", "Device type")
        post_migrate.connect(_load_catalog, sender=self)


def _load_catalog(**kwargs: object) -> None:
    """Bring the device type catalog up to date with the bundled file."""
    from patchmanager.library import catalog

    catalog.load()
