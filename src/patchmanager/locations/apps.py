from django.apps import AppConfig
from django.db.models.signals import post_migrate


class LocationsConfig(AppConfig):
    name = "patchmanager.locations"
    label = "locations"

    def ready(self) -> None:
        from patchmanager.accounts import permissions
        from patchmanager.core.hierarchy import HierarchyNode

        attrs = HierarchyNode.condition_attrs
        permissions.register("locations.roomtype", "Room type")
        permissions.register(
            "locations.site", "Site", extra_actions=("move_tenant",), condition_attrs=attrs
        )
        permissions.register("locations.building", "Building", condition_attrs=attrs)
        permissions.register("locations.floor", "Floor", condition_attrs=attrs)
        permissions.register("locations.room", "Room", condition_attrs=attrs)
        post_migrate.connect(_seed_room_types, sender=self)


def _seed_room_types(**kwargs: object) -> None:
    """Restore the built in room types (a database flush removes the migration's rows)."""
    from importlib import import_module

    from django.apps import apps

    from patchmanager.library.apps import seed_categories

    seed_categories()  # room types refer to categories
    import_module("patchmanager.locations.migrations.0004_room_types").seed(apps, None)
