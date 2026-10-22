from django.apps import AppConfig


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
