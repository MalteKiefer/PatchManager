from django.apps import AppConfig


class DirectoryConfig(AppConfig):
    name = "patchmanager.directory"
    label = "directory"

    def ready(self) -> None:
        from patchmanager.accounts import permissions
        from patchmanager.directory import pages  # noqa: F401  # registers the pages

        permissions.register("directory.organization", "Organization")
        permissions.register("directory.system", "System")
