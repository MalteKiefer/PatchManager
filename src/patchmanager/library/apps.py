from django.apps import AppConfig
from django.db.models.signals import post_migrate


class LibraryConfig(AppConfig):
    name = "patchmanager.library"
    label = "library"

    def ready(self) -> None:
        from patchmanager.accounts import permissions

        permissions.register("library.category", "Device category")

        permissions.register("library.vendor", "Vendor")
        permissions.register("library.devicetype", "Device type")
        post_migrate.connect(_load_catalog, sender=self)
        post_migrate.connect(seed_categories, sender=self)


def _load_catalog(**kwargs: object) -> None:
    """Bring the device type catalog up to date with the bundled file."""
    from patchmanager.library import catalog

    catalog.load()


def seed_categories(**kwargs: object) -> None:
    """Restore the built in categories (a database flush removes the migration's rows)."""
    from importlib import import_module

    from django.apps import apps

    import_module("patchmanager.library.migrations.0005_categories").seed(apps, None)
