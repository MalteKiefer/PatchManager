from django.apps import AppConfig


class ComputeConfig(AppConfig):
    name = "patchmanager.compute"
    label = "compute"

    def ready(self) -> None:
        from patchmanager.accounts import permissions
        from patchmanager.compute import pages  # noqa: F401  # registers the pages

        permissions.register("compute.cluster", "Cluster")
        permissions.register("compute.vm", "Virtual machine")
        permissions.register("compute.storage", "Storage")
