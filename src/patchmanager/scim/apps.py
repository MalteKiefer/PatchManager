from django.apps import AppConfig


class ScimConfig(AppConfig):
    name = "patchmanager.scim"
    label = "scim"
    verbose_name = "SCIM provisioning"

    def ready(self) -> None:
        from patchmanager.accounts import permissions

        permissions.register("system.scim", "SCIM token", actions=("manage",))
