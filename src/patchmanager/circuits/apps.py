from django.apps import AppConfig


class CircuitsConfig(AppConfig):
    name = "patchmanager.circuits"
    label = "circuits"

    def ready(self) -> None:
        from patchmanager.accounts import permissions
        from patchmanager.circuits import pages  # noqa: F401  # registers the pages

        permissions.register("circuits.circuit", "Line")
