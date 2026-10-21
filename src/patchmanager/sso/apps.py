from django.apps import AppConfig


class SsoConfig(AppConfig):
    name = "patchmanager.sso"
    label = "sso"
    verbose_name = "Single sign on"

    def ready(self) -> None:
        from patchmanager.accounts import permissions

        permissions.register("system.sso", "Identity provider", actions=("manage",))
